
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::path::Path;

#[derive(Debug, Clone, Deserialize)]
pub struct RopeScaling {
    #[serde(alias = "type")]
    pub rope_type: String,
    #[serde(default)]
    pub factor: f64,
    #[serde(default)]
    pub low_freq_factor: f64,
    #[serde(default)]
    pub high_freq_factor: f64,
    #[serde(default)]
    pub original_max_position_embeddings: usize,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ModelConfig {
    #[serde(default)]
    pub model_type: String,
    pub vocab_size: usize,
    pub hidden_size: usize,
    pub intermediate_size: usize,
    pub num_hidden_layers: usize,
    pub num_attention_heads: usize,
    #[serde(default)]
    pub num_key_value_heads: Option<usize>,
    #[serde(default)]
    pub head_dim: Option<usize>,
    pub rms_norm_eps: f64,
    #[serde(default = "default_theta")]
    pub rope_theta: f64,
    #[serde(default)]
    pub rope_scaling: Option<RopeScaling>,
    #[serde(default)]
    pub tie_word_embeddings: bool,
    #[serde(default)]
    pub use_sliding_window: bool,
    #[serde(default)]
    pub hidden_act: Option<String>,
    #[serde(default)]
    pub eos_token_id: Option<serde_json::Value>,
}

fn default_theta() -> f64 {
    10_000.0
}

impl ModelConfig {
    pub fn load(dir: &Path) -> Result<Self> {
        let path = dir.join("config.json");
        let text = std::fs::read_to_string(&path).with_context(|| format!("reading {path:?}"))?;
        let cfg: Self = serde_json::from_str(&text).with_context(|| format!("parsing {path:?}"))?;
        cfg.check()?;
        Ok(cfg)
    }

    pub fn check(&self) -> Result<()> {
        if self.use_sliding_window {
            bail!("use_sliding_window=true is not supported");
        }
        if let Some(act) = &self.hidden_act {
            if act != "silu" {
                bail!("hidden_act {act:?} is not supported (only silu)");
            }
        }
        if let Some(rs) = &self.rope_scaling {
            if rs.rope_type != "llama3" && rs.rope_type != "default" {
                bail!("rope_scaling type {:?} is not supported (only llama3)", rs.rope_type);
            }
        }
        if self.num_attention_heads % self.kv_heads() != 0 {
            bail!("num_attention_heads must be a multiple of num_key_value_heads");
        }
        Ok(())
    }

    pub fn kv_heads(&self) -> usize {
        self.num_key_value_heads.unwrap_or(self.num_attention_heads)
    }

    pub fn head_dim(&self) -> usize {
        self.head_dim.unwrap_or(self.hidden_size / self.num_attention_heads)
    }

    pub fn inv_freq(&self) -> Vec<f32> {
        let d = self.head_dim();
        let mut inv: Vec<f64> =
            (0..d / 2).map(|i| 1.0 / self.rope_theta.powf(2.0 * i as f64 / d as f64)).collect();
        if let Some(rs) = self.rope_scaling.as_ref().filter(|r| r.rope_type == "llama3") {
            let old = rs.original_max_position_embeddings as f64;
            let low_wl = old / rs.low_freq_factor;
            let high_wl = old / rs.high_freq_factor;
            for f in inv.iter_mut() {
                let wl = 2.0 * std::f64::consts::PI / *f;
                if wl > low_wl {
                    *f /= rs.factor;
                } else if wl >= high_wl {
                    let s = (old / wl - rs.low_freq_factor) / (rs.high_freq_factor - rs.low_freq_factor);
                    *f = (1.0 - s) * *f / rs.factor + s * *f;
                }
            }
        }
        inv.into_iter().map(|f| f as f32).collect()
    }
}

pub fn eos_ids(dir: &Path, cfg: &ModelConfig) -> Vec<u32> {
    fn ids(v: &serde_json::Value) -> Vec<u32> {
        match v {
            serde_json::Value::Number(n) => n.as_u64().map(|x| vec![x as u32]).unwrap_or_default(),
            serde_json::Value::Array(a) => a.iter().filter_map(|x| x.as_u64()).map(|x| x as u32).collect(),
            _ => vec![],
        }
    }
    let mut out = vec![];
    if let Ok(text) = std::fs::read_to_string(dir.join("generation_config.json")) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(e) = v.get("eos_token_id") {
                out = ids(e);
            }
        }
    }
    if out.is_empty() {
        if let Some(e) = &cfg.eos_token_id {
            out = ids(e);
        }
    }
    out
}
