use anyhow::{bail, Result};
use clap::{Parser, Subcommand};
use manual_rl::compose::{AcceptAll, AcceptAtEnd};
use manual_rl::external::External;
use manual_rl::rollout::{Decider, Judge, Outcome};
use manual_rl::run::{read_episodes, AgentSpec, RunConfig, Runner};
use manual_rl::{checkpoint, train};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Steer rollouts token by token, then train on the corrections")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    Run { config: PathBuf },
    Train {
        config: PathBuf,
        #[arg(long)]
        episodes: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    Ui {
        config: PathBuf,
        #[arg(long, default_value = "127.0.0.1:8765")]
        addr: String,
    },
    Generate {
        config: PathBuf,
        prompt: String,
    },
    Score {
        config: PathBuf,
        text: String,
    },
}

fn agents(cfg: &RunConfig) -> Result<(Box<dyn Decider>, Box<dyn Judge>)> {
    let mut shared: Option<(Vec<String>, External)> = None;
    let mut get = |cmd: &Vec<String>| -> Result<External> {
        if let Some((c, e)) = &shared {
            if c == cmd {
                return Ok(e.clone());
            }
        }
        let e = External::spawn(cmd)?;
        shared = Some((cmd.clone(), e.clone()));
        Ok(e)
    };
    let d: Box<dyn Decider> = match &cfg.decider {
        AgentSpec::Accept => Box::new(AcceptAll),
        AgentSpec::External { command } => Box::new(get(command)?),
    };
    let j: Box<dyn Judge> = match &cfg.judge {
        AgentSpec::Accept => Box::new(AcceptAtEnd),
        AgentSpec::External { command } => Box::new(get(command)?),
    };
    Ok((d, j))
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Run { config } => {
            let cfg = RunConfig::load(&config)?;
            let (mut d, mut j) = agents(&cfg)?;
            Runner::new(cfg)?.run(&mut d, &mut j)
        }
        Cmd::Ui { config, addr } => {
            let mut r = Runner::new(RunConfig::load(&config)?)?;
            manual_rl::serve::serve(&mut r, &addr)
        }
        Cmd::Train { config, episodes, out } => {
            let mut cfg = RunConfig::load(&config)?;
            cfg.run_dir = out.clone();
            let mut t0 = std::time::Instant::now();
            let mut r = Runner::new(cfg)?;
            manual_rl::phase("load model + optimizer", &r.model.device.clone(), &mut t0);
            let eps: Vec<_> = read_episodes(&episodes)?
                .into_iter()
                .map(|x| x.episode)
                .filter(|e| e.outcome == Outcome::Done)
                .collect();
            if eps.is_empty() {
                bail!("no finished episodes in {episodes:?}");
            }
            let mut t0 = std::time::Instant::now();
            let stats = train::update(&mut r.model, &mut r.opt, &eps, &r.cfg.update)?;
            println!("{}", serde_json::to_string_pretty(&stats)?);
            r.state.round += 1;
            checkpoint::save(&out, &r.model, &r.opt, &r.state, &r.cfg.model, 0)?;
            manual_rl::phase("checkpoint", &r.model.device.clone(), &mut t0);
            Ok(())
        }
        Cmd::Generate { config, prompt } => {
            let cfg = RunConfig::load(&config)?;
            let r = Runner::new(cfg)?;
            let p = manual_rl::rollout::PromptItem { id: "cli".into(), prompt, extra: serde_json::Value::Null };
            let ids = r.encode_prompt(&p)?;
            let mut rng = <rand::rngs::StdRng as rand::SeedableRng>::seed_from_u64(r.cfg.seed);
            let t0 = std::time::Instant::now();
            let ep = manual_rl::rollout::run_episode(
                &r.model,
                manual_rl::rollout::Text(Some(&r.tokenizer)),
                &p,
                &ids,
                &mut AcceptAll,
                &mut AcceptAtEnd,
                &r.cfg.episode,
                &mut rng,
            )?;
            let dt = t0.elapsed().as_secs_f64();
            println!("{}", ep.text);
            eprintln!("{} tokens in {dt:.2}s ({:.1} tok/s)", ep.completion.len(), ep.completion.len() as f64 / dt);
            Ok(())
        }
        Cmd::Score { config, text } => {
            let r = Runner::new(RunConfig::load(&config)?)?;
            let ids = r.tokenizer.encode(text, false).map_err(anyhow::Error::msg)?.get_ids().to_vec();
            let pos: Vec<usize> = (1..ids.len()).collect();
            let train: Vec<f32> = r.model.logprobs(&ids, &pos, false)?.to_vec1()?;
            let mut cache = manual_rl::model::KvCache::new(r.model.n_layers());
            let mut sample = vec![];
            let mut logits = r.model.step(&ids[..1], &mut cache)?;
            for &t in &ids[1..] {
                let max = logits.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
                let lse = max + logits.iter().map(|x| (x - max).exp()).sum::<f32>().ln();
                sample.push(logits[t as usize] - lse);
                logits = r.model.step(&[t], &mut cache)?;
            }
            println!("{}", serde_json::json!({"ids": ids, "train": train, "sample": sample}));
            Ok(())
        }
    }
}
