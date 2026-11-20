use crate::model::Model;
use crate::train::AdamW;
use anyhow::{Context, Result};
use candle_core::{Device, Tensor};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RunState {
    pub round: usize,
    pub prompts_used: usize,
    pub adam_t: usize,
}

pub fn save(run: &Path, model: &Model, opt: &AdamW, state: &RunState, model_dir: &Path, keep: usize) -> Result<PathBuf> {
    let name = format!("ckpt-{:05}", state.round);
    let tmp = run.join(format!(".{name}.tmp"));
    let dst = run.join(&name);
    if tmp.exists() {
        std::fs::remove_dir_all(&tmp)?;
    }
    std::fs::create_dir_all(&tmp)?;
    let vars = model.trainable();
    let trainable: HashMap<String, Tensor> =
        vars.iter().map(|(n, v)| (n.clone(), v.as_detached_tensor())).collect();
    candle_core::safetensors::save(&trainable, tmp.join("trainable.safetensors"))?;
    let mut moments = HashMap::new();
    for (i, (n, _)) in vars.iter().enumerate() {
        moments.insert(format!("m.{n}"), opt.m[i].clone());
        moments.insert(format!("v.{n}"), opt.v[i].clone());
    }
    candle_core::safetensors::save(&moments, tmp.join("optimizer.safetensors"))?;
    let st = RunState { adam_t: opt.t, ..state.clone() };
    std::fs::write(tmp.join("state.json"), serde_json::to_string_pretty(&st)?)?;
    if let Some((rank, alpha, targets)) = model.lora_info() {
        let peft: HashMap<String, Tensor> = trainable
            .iter()
            .map(|(n, t)| (format!("base_model.model.{n}"), t.clone()))
            .collect();
        candle_core::safetensors::save(&peft, tmp.join("adapter_model.safetensors"))?;
        let cfg = serde_json::json!({
            "peft_type": "LORA", "task_type": "CAUSAL_LM", "r": rank, "lora_alpha": alpha,
            "target_modules": targets, "lora_dropout": 0.0, "bias": "none",
            "fan_in_fan_out": false, "base_model_name_or_path": model_dir,
        });
        std::fs::write(tmp.join("adapter_config.json"), serde_json::to_string_pretty(&cfg)?)?;
    } else {
        let w: HashMap<String, Tensor> = model.export_weights().into_iter().collect();
        candle_core::safetensors::save(&w, tmp.join("model.safetensors"))?;
        for f in std::fs::read_dir(model_dir)? {
            let p = f?.path();
            let n = p.file_name().unwrap().to_string_lossy().to_string();
            if p.is_file() && (n.ends_with(".json") || n.ends_with(".txt") || n.ends_with(".model"))
                && !n.ends_with(".index.json")
            {
                std::fs::copy(&p, tmp.join(&n))?;
            }
        }
    }
    if dst.exists() {
        std::fs::remove_dir_all(&dst)?;
    }
    std::fs::rename(&tmp, &dst)?;
    std::fs::write(run.join("LATEST"), &name)?;
    prune(run, keep)?;
    Ok(dst)
}

fn prune(run: &Path, keep: usize) -> Result<()> {
    if keep == 0 {
        return Ok(());
    }
    let mut ck: Vec<PathBuf> = std::fs::read_dir(run)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("ckpt-")))
        .collect();
    ck.sort();
    while ck.len() > keep {
        std::fs::remove_dir_all(ck.remove(0))?;
    }
    Ok(())
}

pub fn resume(run: &Path, model: &mut Model, opt: &mut AdamW) -> Result<Option<RunState>> {
    let latest = run.join("LATEST");
    if !latest.exists() {
        return Ok(None);
    }
    let dir = run.join(std::fs::read_to_string(&latest)?.trim());
    let vals = candle_core::safetensors::load(dir.join("trainable.safetensors"), &Device::Cpu)?;
    model.load_trainable(&vals)?;
    let mom = candle_core::safetensors::load(dir.join("optimizer.safetensors"), &opt.device)?;
    for (i, (n, _)) in model.trainable().iter().enumerate() {
        opt.m[i] = mom.get(&format!("m.{n}")).with_context(|| format!("optimizer lacks m.{n}"))?.clone();
        opt.v[i] = mom.get(&format!("v.{n}")).with_context(|| format!("optimizer lacks v.{n}"))?.clone();
    }
    let st: RunState = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json"))?)?;
    opt.t = st.adam_t;
    Ok(Some(st))
}
