
use crate::checkpoint::{self, RunState};
use crate::config::eos_ids;
use crate::model::{Adapt, Model};
use crate::rollout::{run_episode, Decider, Episode, EpisodeConfig, Judge, Outcome, PromptItem, Text};
use crate::train::{update, AdamW, UpdateConfig, UpdateStats};
use anyhow::{bail, Context, Result};
use candle_core::{DType, Device};
use rand::SeedableRng;
use serde::{Deserialize, Serialize};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use tokenizers::Tokenizer;

pub const CHATML: &str = "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum AgentSpec {
    #[default]
    Accept,
    External { command: Vec<String> },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunConfig {
    pub model: PathBuf,
    pub run_dir: PathBuf,
    pub prompts: PathBuf,
    #[serde(default = "default_template")]
    pub template: String,
    #[serde(default = "default_device")]
    pub device: String,
    #[serde(default = "default_dtype")]
    pub dtype: String,
    #[serde(default)]
    pub seed: u64,
    #[serde(default = "default_max_positions")]
    pub max_positions: usize,
    #[serde(default)]
    pub episode: EpisodeConfig,
    #[serde(default)]
    pub adapt: Adapt,
    #[serde(default)]
    pub update: UpdateConfig,
    #[serde(default = "one")]
    pub episodes_per_round: usize,
    #[serde(default)]
    pub rounds: usize,
    #[serde(default = "one")]
    pub passes: usize,
    #[serde(default = "default_keep")]
    pub keep_checkpoints: usize,
    #[serde(default)]
    pub decider: AgentSpec,
    #[serde(default)]
    pub judge: AgentSpec,
}

fn default_template() -> String {
    CHATML.into()
}
fn default_device() -> String {
    "cuda:0".into()
}
fn default_dtype() -> String {
    "bf16".into()
}
fn default_max_positions() -> usize {
    8192
}
fn one() -> usize {
    1
}
fn default_keep() -> usize {
    3
}

impl RunConfig {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path).with_context(|| format!("reading {path:?}"))?;
        toml::from_str(&text).with_context(|| format!("parsing {path:?}"))
    }
}

pub fn device(spec: &str) -> Result<Device> {
    match spec {
        "cpu" => Ok(Device::Cpu),
        s if s.starts_with("cuda") => {
            let n = s.strip_prefix("cuda:").map(|n| n.parse()).transpose()?.unwrap_or(0);
            Ok(Device::new_cuda(n)?)
        }
        s => bail!("unknown device {s:?} (cpu or cuda:N)"),
    }
}

pub fn dtype(spec: &str) -> Result<DType> {
    Ok(match spec {
        "bf16" => DType::BF16,
        "f16" => DType::F16,
        "f32" => DType::F32,
        s => bail!("unknown dtype {s:?} (bf16, f16, f32)"),
    })
}

pub fn load_prompts(path: &Path) -> Result<Vec<PromptItem>> {
    let f = std::fs::File::open(path).with_context(|| format!("opening {path:?}"))?;
    let mut out = vec![];
    for (i, line) in std::io::BufReader::new(f).lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let mut v: serde_json::Value =
            serde_json::from_str(&line).with_context(|| format!("{path:?} line {}", i + 1))?;
        let obj = v.as_object_mut().context("each prompt line must be a JSON object")?;
        let prompt = obj.remove("prompt").and_then(|p| p.as_str().map(String::from))
            .with_context(|| format!("{path:?} line {}: no \"prompt\" string", i + 1))?;
        let id = obj.remove("id").map(|x| x.as_str().map(String::from).unwrap_or(x.to_string()))
            .unwrap_or_else(|| format!("{i}"));
        let extra = if obj.is_empty() { serde_json::Value::Null } else { serde_json::Value::Object(obj.clone()) };
        out.push(PromptItem { id, prompt, extra });
    }
    if out.is_empty() {
        bail!("{path:?} holds no prompts");
    }
    Ok(out)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EpisodeRecord {
    pub round: usize,
    pub index: usize,
    pub episode: Episode,
}

pub fn read_episodes(path: &Path) -> Result<Vec<EpisodeRecord>> {
    if !path.exists() {
        return Ok(vec![]);
    }
    let f = std::fs::File::open(path)?;
    let mut out = vec![];
    for line in std::io::BufReader::new(f).lines() {
        let line = line?;
        // A line cut short by a preemption is the only kind that fails to parse; skip it.
        if let Ok(r) = serde_json::from_str(&line) {
            out.push(r);
        }
    }
    Ok(out)
}

pub fn append(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(path)?;
    writeln!(f, "{}", serde_json::to_string(value)?)?;
    f.sync_data()?;
    Ok(())
}

pub struct Runner {
    pub cfg: RunConfig,
    pub model: Model,
    pub tokenizer: Tokenizer,
    pub opt: AdamW,
    pub prompts: Vec<PromptItem>,
    pub state: RunState,
}

impl Runner {
    pub fn new(cfg: RunConfig) -> Result<Self> {
        let dev = device(&cfg.device)?;
        let mut model = Model::load(&cfg.model, cfg.adapt.clone(), dtype(&cfg.dtype)?, &dev, cfg.max_positions, cfg.seed)?;
        let tokenizer = Tokenizer::from_file(cfg.model.join("tokenizer.json")).map_err(anyhow::Error::msg)?;
        let prompts = load_prompts(&cfg.prompts)?;
        std::fs::create_dir_all(&cfg.run_dir)?;
        std::fs::write(cfg.run_dir.join("config.json"), serde_json::to_string_pretty(&cfg)?)?;
        let on_cpu = cfg.update.optimizer_on_cpu.unwrap_or(matches!(cfg.adapt, Adapt::Full));
        let mut opt = AdamW::new_on(&model.trainable(), if on_cpu { &Device::Cpu } else { &dev })?;
        let state = checkpoint::resume(&cfg.run_dir, &mut model, &mut opt)?.unwrap_or_default();
        let mut cfg = cfg;
        if cfg.episode.eos.is_empty() {
            cfg.episode.eos = eos_ids(&cfg.model, &model.cfg);
        }
        if cfg.episode.eos.is_empty() {
            bail!("no EOS token ids in the model directory; set episode.eos");
        }
        Ok(Self { cfg, model, tokenizer, opt, prompts, state })
    }

    pub fn encode_prompt(&self, p: &PromptItem) -> Result<Vec<u32>> {
        let text = self.cfg.template.replace("{prompt}", &p.prompt);
        Ok(self.tokenizer.encode(text, false).map_err(anyhow::Error::msg)?.get_ids().to_vec())
    }

    pub fn run(&mut self, decider: &mut dyn Decider, judge: &mut dyn Judge) -> Result<()> {
        let run = self.cfg.run_dir.clone();
        let log = run.join("episodes.jsonl");
        let prior = read_episodes(&log)?;
        let mut used = prior.len();
        let mut pending: Vec<Episode> = prior
            .into_iter()
            .filter(|r| r.round == self.state.round && r.episode.outcome == Outcome::Done)
            .map(|r| r.episode)
            .collect();
        let budget = self.prompts.len() * self.cfg.passes;
        let lr = self.cfg.update.lr_for(&self.model);
        eprintln!(
            "manual-rl: {} ({}), lr {lr}, round {}, {} prompts used, {} episodes pending",
            self.cfg.model.display(),
            match &self.cfg.adapt { Adapt::Full => "full".to_string(), a => format!("{a:?}") },
            self.state.round, used, pending.len(),
        );
        loop {
            if self.cfg.rounds > 0 && self.state.round >= self.cfg.rounds {
                break;
            }
            let mut out_of_prompts = false;
            while pending.len() < self.cfg.episodes_per_round {
                if used >= budget {
                    out_of_prompts = true;
                    break;
                }
                let p = self.prompts[used % self.prompts.len()].clone();
                let ids = self.encode_prompt(&p)?;
                let mut rng = rand::rngs::StdRng::seed_from_u64(self.cfg.seed.wrapping_mul(0x9E3779B97F4A7C15) ^ used as u64);
                let ep = run_episode(&self.model, Text(Some(&self.tokenizer)), &p, &ids, decider, judge, &self.cfg.episode, &mut rng)?;
                eprintln!(
                    "episode {used} [{}]: {:?}, {} tokens, {} forks, {} revisions",
                    p.id, ep.outcome, ep.completion.len(), ep.forks.len(), ep.revisions
                );
                append(&log, &EpisodeRecord { round: self.state.round, index: used, episode: ep.clone() })?;
                used += 1;
                if ep.outcome == Outcome::Done {
                    pending.push(ep);
                }
            }
            if pending.is_empty() {
                break;
            }
            let stats: UpdateStats = update(&mut self.model, &mut self.opt, &pending, &self.cfg.update)?;
            eprintln!("round {}: {}", self.state.round, serde_json::to_string(&stats)?);
            append(&run.join("metrics.jsonl"), &serde_json::json!({"round": self.state.round, "stats": stats}))?;
            self.state.round += 1;
            self.state.prompts_used = used;
            checkpoint::save(&run, &self.model, &self.opt, &self.state, &self.cfg.model, self.cfg.keep_checkpoints)?;
            pending.clear();
            if out_of_prompts {
                break;
            }
        }
        Ok(())
    }
}
