use crate::model::Model;
use crate::rollout::{Episode, Outcome};
use anyhow::{bail, Result};
use candle_core::{DType, Device, Tensor, Var};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct UpdateConfig {
    pub lr: Option<f64>,
    pub beta: f64,
    pub pair_weight: f64,
    pub sft_weight: f64,
    pub forced_weight: f64,
    pub pair_horizon: usize,
    pub epochs: usize,
    pub minibatch: usize,
    pub max_grad_norm: f64,
    pub weight_decay: f64,
    pub adam_beta1: f64,
    pub adam_beta2: f64,
    pub adam_eps: f64,
    pub optimizer_on_cpu: Option<bool>,
    pub optimizer: String,
    pub momentum: f64,
}

impl Default for UpdateConfig {
    fn default() -> Self {
        Self {
            lr: None,
            beta: 0.1,
            pair_weight: 1.0,
            sft_weight: 0.0,
            forced_weight: 0.0,
            pair_horizon: 256,
            epochs: 1,
            minibatch: 8,
            max_grad_norm: 1.0,
            weight_decay: 0.0,
            adam_beta1: 0.9,
            adam_beta2: 0.999,
            adam_eps: 1e-8,
            optimizer_on_cpu: None,
            optimizer: "adamw".to_string(),
            momentum: 0.9,
        }
    }
}

impl UpdateConfig {
    pub fn lr_for(&self, model: &Model) -> f64 {
        self.lr.unwrap_or(if model.lora_info().is_some() { 5e-5 } else { 1e-6 })
    }
}

struct Pair {
    chosen: Vec<usize>,
    rejected_seq: Vec<u32>,
    rejected: Vec<usize>,
}

struct Item {
    full: Vec<u32>,
    pairs: Vec<Pair>,
    sft: Vec<usize>,
}

impl Item {
    fn new(ep: &Episode, horizon: usize) -> Self {
        let pl = ep.prompt_tokens.len();
        let mut full = ep.prompt_tokens.clone();
        full.extend(&ep.completion);
        let pairs = ep
            .forks
            .iter()
            .map(|f| {
                let nc = f.forced.len().min(horizon);
                let nr = f.rejected.len().min(horizon);
                let mut rejected_seq = full[..pl + f.pos].to_vec();
                rejected_seq.extend(&f.rejected[..nr]);
                Pair {
                    chosen: (0..nc).map(|j| pl + f.pos + j).collect(),
                    rejected_seq,
                    rejected: (0..nr).map(|j| pl + f.pos + j).collect(),
                }
            })
            .collect();
        let sft = (pl..full.len()).collect();
        Self { full, pairs, sft }
    }
}

fn group_logprobs(model: &Model, ids: &[u32], groups: &[&[usize]]) -> Result<Vec<f64>> {
    let mut pos: Vec<usize> = groups.iter().flat_map(|g| g.iter().copied()).collect();
    if pos.is_empty() {
        return Ok(vec![0.0; groups.len()]);
    }
    pos.sort_unstable();
    pos.dedup();
    let lp: Vec<f32> = model.logprobs(ids, &pos, false)?.to_vec1()?;
    Ok(groups
        .iter()
        .map(|g| g.iter().map(|p| lp[pos.binary_search(p).unwrap()] as f64).sum())
        .collect())
}

fn pair_logprobs(model: &Model, items: &[&Item]) -> Result<Vec<Vec<(f64, f64)>>> {
    items
        .iter()
        .map(|it| {
            let groups: Vec<&[usize]> = it.pairs.iter().map(|p| p.chosen.as_slice()).collect();
            let chosen = group_logprobs(model, &it.full, &groups)?;
            it.pairs
                .iter()
                .zip(chosen)
                .map(|(p, c)| Ok((c, group_logprobs(model, &p.rejected_seq, &[&p.rejected])?[0])))
                .collect()
        })
        .collect()
}

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

fn softplus(x: f64) -> f64 {
    if x > 30.0 { x } else { x.exp().ln_1p() }
}

pub struct AdamW {
    pub m: Vec<Tensor>,
    pub v: Vec<Tensor>,
    pub t: usize,
    pub device: Device,
}

impl AdamW {
    pub fn new(vars: &[(String, Var)]) -> Result<Self> {
        let dev = vars.first().map_or(Device::Cpu, |(_, v)| v.device().clone());
        Self::new_on(vars, &dev)
    }

    pub fn new_on(vars: &[(String, Var)], device: &Device) -> Result<Self> {
        let z = |v: &Var| Tensor::zeros(v.shape(), DType::F32, device);
        Ok(Self {
            m: vars.iter().map(|(_, v)| z(v)).collect::<candle_core::Result<_>>()?,
            v: vars.iter().map(|(_, v)| z(v)).collect::<candle_core::Result<_>>()?,
            t: 0,
            device: device.clone(),
        })
    }

    fn step(&mut self, vars: &[(String, Var)], grads: &[Option<Tensor>], lr: f64, c: &UpdateConfig) -> Result<()> {
        self.t += 1;
        if c.optimizer == "sgdm" {
            for (i, (_, var)) in vars.iter().enumerate() {
                let Some(g) = &grads[i] else { continue };
                let g = g.to_dtype(DType::F32)?.to_device(&self.device)?;
                self.m[i] = ((&self.m[i] * c.momentum)? + g)?;
                let upd = self.m[i].to_device(var.device())?;
                let mut w = var.as_detached_tensor();
                if c.weight_decay > 0.0 {
                    w = (w * (1.0 - lr * c.weight_decay))?;
                }
                var.set(&(w - (upd * lr)?)?)?;
            }
            return Ok(());
        }
        if c.optimizer != "adamw" {
            bail!("unknown optimizer {:?} (adamw or sgdm)", c.optimizer);
        }
        let (b1, b2) = (c.adam_beta1, c.adam_beta2);
        let bc1 = 1.0 - b1.powi(self.t as i32);
        let bc2 = 1.0 - b2.powi(self.t as i32);
        for (i, (_, var)) in vars.iter().enumerate() {
            let Some(g) = &grads[i] else { continue };
            let upd = if self.device.is_cpu() {
                let (m, v, u) = adam_cpu(&self.m[i], &self.v[i], g, b1, b2, bc1, bc2, c.adam_eps)?;
                (self.m[i], self.v[i]) = (m, v);
                u.to_device(var.device())?
            } else {
                self.m[i] = ((&self.m[i] * b1)? + (g * (1.0 - b1))?)?;
                self.v[i] = ((&self.v[i] * b2)? + (g.sqr()? * (1.0 - b2))?)?;
                let mhat = (&self.m[i] / bc1)?;
                let vhat = (&self.v[i] / bc2)?;
                mhat.div(&(vhat.sqrt()? + c.adam_eps)?)?
            };
            let mut w = var.as_detached_tensor();
            if c.weight_decay > 0.0 {
                w = (w * (1.0 - lr * c.weight_decay))?;
            }
            var.set(&(w - (upd * lr)?)?)?;
        }
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
pub fn adam_cpu(m: &Tensor, v: &Tensor, g: &Tensor, b1: f64, b2: f64, bc1: f64, bc2: f64, eps: f64)
    -> Result<(Tensor, Tensor, Tensor)> {
    use rayon::prelude::*;
    let shape = m.shape().clone();
    let mut mv = m.flatten_all()?.to_vec1::<f32>()?;
    let mut vv = v.flatten_all()?.to_vec1::<f32>()?;
    let mut gv = g.to_dtype(DType::F32)?.flatten_all()?.to_vec1::<f32>()?;
    let (b1, omb1, b2, omb2) = (b1 as f32, (1.0 - b1) as f32, b2 as f32, (1.0 - b2) as f32);
    let (ibc1, ibc2, eps) = ((1.0 / bc1) as f32, (1.0 / bc2) as f32, eps as f32);
    const CHUNK: usize = 1 << 16;
    mv.par_chunks_mut(CHUNK).zip(vv.par_chunks_mut(CHUNK)).zip(gv.par_chunks_mut(CHUNK)).for_each(|((m, v), g)| {
        for j in 0..g.len() {
            let gj = g[j];
            m[j] = m[j] * b1 + gj * omb1;
            v[j] = v[j] * b2 + (gj * gj) * omb2;
            g[j] = (m[j] * ibc1) / ((v[j] * ibc2).sqrt() + eps);
        }
    });
    let dev = Device::Cpu;
    Ok((Tensor::from_vec(mv, shape.clone(), &dev)?, Tensor::from_vec(vv, shape.clone(), &dev)?,
        Tensor::from_vec(gv, shape, &dev)?))
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct UpdateStats {
    pub episodes: usize,
    pub pairs: usize,
    pub steps: usize,
    pub lr: f64,
    pub pair_loss: f64,
    pub margin_before: f64,
    pub margin_after: f64,
    pub margin_up: f64,
    pub sft_logp_before: f64,
    pub sft_logp_after: f64,
    pub forced_logp_before: f64,
    pub forced_logp_after: f64,
    pub grad_norm: f64,
}

fn mean(xs: impl Iterator<Item = f64>) -> f64 {
    let (s, n) = xs.fold((0.0, 0usize), |(s, n), x| (s + x, n + 1));
    if n == 0 { 0.0 } else { s / n as f64 }
}

fn forced_logp(lp: &[Vec<(f64, f64)>], items: &[&Item]) -> f64 {
    let n: usize = items.iter().flat_map(|it| &it.pairs).map(|p| p.chosen.len()).sum();
    if n == 0 {
        return 0.0;
    }
    lp.iter().flatten().map(|(c, _)| c).sum::<f64>() / n as f64
}

fn sft_logp(model: &Model, items: &[&Item]) -> Result<f64> {
    let per: Vec<f64> = items
        .iter()
        .filter(|it| !it.sft.is_empty())
        .map(|it| Ok(group_logprobs(model, &it.full, &[&it.sft])?[0] / it.sft.len() as f64))
        .collect::<Result<_>>()?;
    Ok(mean(per.into_iter()))
}

pub fn update(model: &mut Model, opt: &mut AdamW, episodes: &[Episode], cfg: &UpdateConfig) -> Result<UpdateStats> {
    let eps: Vec<&Episode> = episodes.iter().filter(|e| e.outcome == Outcome::Done).collect();
    let items: Vec<Item> = eps.iter().map(|e| Item::new(e, cfg.pair_horizon)).collect();
    let all: Vec<&Item> = items.iter().collect();
    let vars = model.trainable();
    if vars.len() != opt.m.len() {
        bail!("optimizer state does not match the model's trainable parameters");
    }
    let lr = cfg.lr_for(model);
    let mut st = UpdateStats { episodes: items.len(), lr, ..Default::default() };
    st.pairs = items.iter().map(|it| it.pairs.len()).sum();
    let use_pairs = cfg.pair_weight > 0.0 && st.pairs > 0;
    let use_sft = cfg.sft_weight > 0.0;
    let use_forced = cfg.forced_weight > 0.0 && st.pairs > 0;
    if items.is_empty() || (!use_pairs && !use_sft && !use_forced) {
        return Ok(st);
    }
    let dev = model.device.clone();
    let mut t0 = std::time::Instant::now();
    let reference = pair_logprobs(model, &all)?;
    crate::phase("reference log-probs", &dev, &mut t0);
    st.margin_before = mean(reference.iter().flatten().map(|(c, r)| c - r));
    st.sft_logp_before = sft_logp(model, &all)?;
    st.forced_logp_before = forced_logp(&reference, &all);
    let mut losses = vec![];
    let mut norms = vec![];
    for _ in 0..cfg.epochs {
        for (mb_i, mb) in all.chunks(cfg.minibatch.max(1)).enumerate() {
            let base = mb_i * cfg.minibatch.max(1);
            let refs = &reference[base..base + mb.len()];
            let now = if st.steps == 0 { refs.to_vec() } else { pair_logprobs(model, mb)? };
            let n_pairs: usize = mb.iter().map(|it| it.pairs.len()).sum();
            let n_sft = mb.iter().filter(|it| !it.sft.is_empty()).count();
            let n_forced: usize = mb.iter().flat_map(|it| &it.pairs).map(|p| p.chosen.len()).sum();
            let mut acc: Vec<Option<Tensor>> = vec![None; vars.len()];
            let mut add = |ids: &[u32], pos: &[usize], wt: &[f32]| -> Result<()> {
                model.logprob_grads_into(ids, pos, wt, &mut acc)?;
                Ok(())
            };
            for (k, it) in mb.iter().enumerate() {
                let mut w: std::collections::BTreeMap<usize, f64> = Default::default();
                let mut rej_coef = vec![0.0; it.pairs.len()];
                if use_pairs {
                    for (j, p) in it.pairs.iter().enumerate() {
                        let (c0, r0) = refs[k][j];
                        let (c, r) = now[k][j];
                        let m = cfg.beta * ((c - c0) - (r - r0));
                        losses.push(softplus(-m));
                        let s = cfg.pair_weight * cfg.beta * sigmoid(-m) / n_pairs as f64;
                        for &q in &p.chosen {
                            *w.entry(q).or_default() -= s;
                        }
                        rej_coef[j] = s;
                    }
                }
                if use_forced && n_forced > 0 {
                    let s = cfg.forced_weight / n_forced as f64;
                    for p in &it.pairs {
                        for &q in &p.chosen {
                            *w.entry(q).or_default() -= s;
                        }
                    }
                }
                if use_sft && !it.sft.is_empty() {
                    let s = cfg.sft_weight / (n_sft as f64 * it.sft.len() as f64);
                    for &q in &it.sft {
                        *w.entry(q).or_default() -= s;
                    }
                }
                if !w.is_empty() {
                    let pos: Vec<usize> = w.keys().copied().collect();
                    let wt: Vec<f32> = w.values().map(|&x| x as f32).collect();
                    add(&it.full, &pos, &wt)?;
                }
                for (j, p) in it.pairs.iter().enumerate() {
                    if rej_coef[j] != 0.0 {
                        add(&p.rejected_seq, &p.rejected, &vec![rej_coef[j] as f32; p.rejected.len()])?;
                    }
                }
            }
            crate::phase("gradients (forward+backward)", &dev, &mut t0);
            let mut sq = 0.0f64;
            for g in acc.iter().flatten() {
                sq += g.to_dtype(DType::F32)?.sqr()?.sum_all()?.to_scalar::<f32>()? as f64;
            }
            let norm = sq.sqrt();
            norms.push(norm);
            if cfg.max_grad_norm > 0.0 && norm > cfg.max_grad_norm {
                let k = cfg.max_grad_norm / norm;
                for g in acc.iter_mut().flatten() {
                    *g = (&*g * k)?;
                }
            }
            crate::phase("grad norm + clip", &dev, &mut t0);
            opt.step(&vars, &acc, lr, cfg)?;
            crate::phase("optimizer step", &dev, &mut t0);
            model.refresh()?;
            crate::phase("refresh bf16 copies", &dev, &mut t0);
            st.steps += 1;
        }
    }
    st.pair_loss = mean(losses.into_iter());
    st.grad_norm = mean(norms.into_iter());
    let after = pair_logprobs(model, &all)?;
    crate::phase("after log-probs", &dev, &mut t0);
    st.margin_after = mean(after.iter().flatten().map(|(c, r)| c - r));
    let ups: Vec<f64> = reference
        .iter()
        .flatten()
        .zip(after.iter().flatten())
        .map(|((c0, r0), (c1, r1))| if c1 - r1 > c0 - r0 { 1.0 } else { 0.0 })
        .collect();
    st.margin_up = mean(ups.into_iter());
    st.sft_logp_after = sft_logp(model, &all)?;
    st.forced_logp_after = forced_logp(&after, &all);
    Ok(st)
}
