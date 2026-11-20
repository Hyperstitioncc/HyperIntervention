
use crate::config::ModelConfig;
use anyhow::{bail, Context, Result};
use candle_core::{DType, Device, IndexOp, Tensor, Var, D};
use rand::{Rng, SeedableRng};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "mode", rename_all = "lowercase")]
pub enum Adapt {
    Lora {
        #[serde(default = "default_rank")]
        rank: usize,
        #[serde(default = "default_alpha")]
        alpha: f64,
        #[serde(default)]
        targets: Vec<String>,
    },
    Full,
}

fn default_rank() -> usize {
    16
}
fn default_alpha() -> f64 {
    32.0
}

pub const ALL_PROJECTIONS: [&str; 7] =
    ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"];

impl Default for Adapt {
    fn default() -> Self {
        Adapt::Lora { rank: default_rank(), alpha: default_alpha(), targets: vec![] }
    }
}

impl Adapt {
    fn lora_targets(&self) -> Vec<String> {
        match self {
            Adapt::Lora { targets, .. } if targets.is_empty() => {
                ALL_PROJECTIONS.iter().map(|s| s.to_string()).collect()
            }
            Adapt::Lora { targets, .. } => targets.clone(),
            Adapt::Full => vec![],
        }
    }
}

pub struct Param {
    compute: Tensor,
    master: Option<Var>,
}

impl Param {
    fn frozen(t: Tensor) -> Self {
        Self { compute: t, master: None }
    }

    fn trainable(master: Tensor, dtype: DType) -> Result<Self> {
        let master = Var::from_tensor(&master.to_dtype(DType::F32)?)?;
        let compute = master.as_detached_tensor().to_dtype(dtype)?;
        Ok(Self { compute, master: Some(master) })
    }

    fn get(&self, grad: bool) -> Result<Tensor> {
        match (&self.master, grad) {
            (Some(m), true) => Ok(m.as_tensor().to_dtype(self.compute.dtype())?),
            _ => Ok(self.compute.clone()),
        }
    }

    fn refresh(&mut self) -> Result<()> {
        if let Some(m) = &self.master {
            self.compute = m.as_detached_tensor().to_dtype(self.compute.dtype())?;
        }
        Ok(())
    }
}

struct Lora {
    a: Param,
    b: Param,
    scale: f64,
}

struct Linear {
    name: String,
    w: Param,
    b: Option<Param>,
    lora: Option<Lora>,
}

impl Linear {
    fn forward(&self, x: &Tensor, grad: bool) -> Result<Tensor> {
        let mut y = x.matmul(&self.w.get(grad)?.t()?)?;
        if let Some(b) = &self.b {
            y = y.broadcast_add(&b.get(grad)?)?;
        }
        if let Some(l) = &self.lora {
            let h = x.matmul(&l.a.get(grad)?.t()?)?;
            let d = h.matmul(&l.b.get(grad)?.t()?)?;
            y = (y + (d * l.scale)?)?;
        }
        Ok(y)
    }
}

struct Layer {
    ln1: Param,
    q: Linear,
    k: Linear,
    v: Linear,
    o: Linear,
    q_norm: Option<Param>,
    k_norm: Option<Param>,
    ln2: Param,
    gate: Linear,
    up: Linear,
    down: Linear,
}

pub struct KvCache {
    layers: Vec<Option<(Tensor, Tensor)>>,
    len: usize,
}

impl KvCache {
    pub fn new(n_layers: usize) -> Self {
        Self { layers: (0..n_layers).map(|_| None).collect(), len: 0 }
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn truncate(&mut self, n: usize) -> Result<()> {
        if n >= self.len {
            return Ok(());
        }
        for slot in self.layers.iter_mut() {
            if let Some((k, v)) = slot.take() {
                if n > 0 {
                    *slot = Some((k.narrow(2, 0, n)?.contiguous()?, v.narrow(2, 0, n)?.contiguous()?));
                }
            }
        }
        self.len = n;
        Ok(())
    }
}

pub trait WeightSource {
    fn get(&self, name: &str) -> Result<Option<Tensor>>;
}

impl WeightSource for HashMap<String, Tensor> {
    fn get(&self, name: &str) -> Result<Option<Tensor>> {
        Ok(HashMap::get(self, name).cloned())
    }
}

struct Safetensors {
    st: candle_core::safetensors::MmapedSafetensors,
    names: std::collections::HashSet<String>,
    device: Device,
}

impl WeightSource for Safetensors {
    fn get(&self, name: &str) -> Result<Option<Tensor>> {
        if !self.names.contains(name) {
            return Ok(None);
        }
        Ok(Some(self.st.load(name, &self.device)?))
    }
}

struct Frame {
    cos: Tensor,
    sin: Tensor,
    mask: Option<Tensor>,
}

pub struct Model {
    pub cfg: ModelConfig,
    pub dtype: DType,
    pub device: Device,
    pub adapt: Adapt,
    embed: Param,
    layers: Vec<Layer>,
    norm: Param,
    lm_head: Option<Param>, // None: tied to `embed`
    cos: Tensor,
    sin: Tensor,
    max_pos: usize,
}

impl Model {
    pub fn load(dir: &Path, adapt: Adapt, dtype: DType, device: &Device, max_pos: usize, seed: u64) -> Result<Self> {
        let cfg = ModelConfig::load(dir)?;
        let mut files: Vec<_> = std::fs::read_dir(dir)?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().is_some_and(|x| x == "safetensors"))
            .collect();
        files.sort();
        if files.is_empty() {
            bail!("no .safetensors files in {dir:?}");
        }
        let st = unsafe { candle_core::safetensors::MmapedSafetensors::multi(&files)? };
        let names = st.tensors().into_iter().map(|(n, _)| n).collect();
        let src = Safetensors { st, names, device: device.clone() };
        Self::build(cfg, &src, adapt, dtype, device, max_pos, seed)
    }

    pub fn build(
        cfg: ModelConfig,
        src: &dyn WeightSource,
        adapt: Adapt,
        dtype: DType,
        device: &Device,
        max_pos: usize,
        seed: u64,
    ) -> Result<Self> {
        cfg.check()?;
        let full = adapt == Adapt::Full;
        let targets = adapt.lora_targets();
        let (rank, alpha) = match &adapt {
            Adapt::Lora { rank, alpha, .. } => (*rank, *alpha),
            Adapt::Full => (0, 0.0),
        };
        if !full && rank == 0 {
            bail!("lora rank must be >= 1");
        }
        for t in &targets {
            if !ALL_PROJECTIONS.contains(&t.as_str()) {
                bail!("unknown lora target {t:?}; expected one of {ALL_PROJECTIONS:?}");
            }
        }
        let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
        let need = |name: &str| -> Result<Tensor> {
            src.get(name)?.with_context(|| format!("missing weight {name}"))
        };
        let param = |t: Tensor| -> Result<Param> {
            if full {
                Param::trainable(t, dtype)
            } else {
                Ok(Param::frozen(t.to_dtype(dtype)?))
            }
        };
        let mut linear = |module: String, short: &str| -> Result<Linear> {
            let w = need(&format!("{module}.weight"))?;
            let b = src.get(&format!("{module}.bias"))?;
            let (out_f, in_f) = w.dims2()?;
            let lora = if targets.iter().any(|t| t == short) {
                // A ~ U(-1/sqrt(in), 1/sqrt(in)) (kaiming-uniform, a=sqrt(5)), B = 0: the adapter
                // starts as the identity edit. Drawn on the CPU so a seed means the same init on
                // every device.
                let bound = 1.0 / (in_f as f32).sqrt();
                let a: Vec<f32> = (0..rank * in_f).map(|_| rng.gen_range(-bound..bound)).collect();
                let a = Tensor::from_vec(a, (rank, in_f), &Device::Cpu)?.to_device(device)?;
                let b0 = Tensor::zeros((out_f, rank), DType::F32, device)?;
                Some(Lora {
                    a: Param::trainable(a, dtype)?,
                    b: Param::trainable(b0, dtype)?,
                    scale: alpha / rank as f64,
                })
            } else {
                None
            };
            Ok(Linear { name: module, w: param(w)?, b: b.map(param).transpose()?, lora })
        };
        let mut layers = Vec::with_capacity(cfg.num_hidden_layers);
        for i in 0..cfg.num_hidden_layers {
            let p = format!("model.layers.{i}");
            layers.push(Layer {
                ln1: param(need(&format!("{p}.input_layernorm.weight"))?)?,
                q: linear(format!("{p}.self_attn.q_proj"), "q_proj")?,
                k: linear(format!("{p}.self_attn.k_proj"), "k_proj")?,
                v: linear(format!("{p}.self_attn.v_proj"), "v_proj")?,
                o: linear(format!("{p}.self_attn.o_proj"), "o_proj")?,
                q_norm: src.get(&format!("{p}.self_attn.q_norm.weight"))?.map(param).transpose()?,
                k_norm: src.get(&format!("{p}.self_attn.k_norm.weight"))?.map(param).transpose()?,
                ln2: param(need(&format!("{p}.post_attention_layernorm.weight"))?)?,
                gate: linear(format!("{p}.mlp.gate_proj"), "gate_proj")?,
                up: linear(format!("{p}.mlp.up_proj"), "up_proj")?,
                down: linear(format!("{p}.mlp.down_proj"), "down_proj")?,
            });
        }
        let embed = param(need("model.embed_tokens.weight")?)?;
        let norm = param(need("model.norm.weight")?)?;
        let lm_head = match src.get("lm_head.weight")? {
            Some(w) if !cfg.tie_word_embeddings => Some(param(w)?),
            _ => None,
        };
        let inv = cfg.inv_freq();
        let half = inv.len();
        let mut freqs = Vec::with_capacity(max_pos * half);
        for p in 0..max_pos {
            for f in &inv {
                freqs.push(p as f32 * f);
            }
        }
        let freqs = Tensor::from_vec(freqs, (max_pos, half), device)?;
        let cos = freqs.cos()?.to_dtype(dtype)?;
        let sin = freqs.sin()?.to_dtype(dtype)?;
        Ok(Self { cfg, dtype, device: device.clone(), adapt, embed, layers, norm, lm_head, cos, sin, max_pos })
    }

    pub fn n_layers(&self) -> usize {
        self.layers.len()
    }

    pub fn max_positions(&self) -> usize {
        self.max_pos
    }

    fn rms(x: &Tensor, w: &Param, eps: f64, fast: bool, grad: bool) -> Result<Tensor> {
        let w = w.get(grad)?;
        Ok(if fast {
            candle_nn::ops::rms_norm(&x.contiguous()?, &w, eps as f32)?
        } else {
            candle_nn::ops::rms_norm_slow(x, &w, eps as f32)?
        })
    }

    fn frame(&self, off: usize, t: usize) -> Result<Frame> {
        if off + t > self.max_pos {
            bail!("sequence of {} positions exceeds max_positions {}", off + t, self.max_pos);
        }
        let cos = self.cos.narrow(0, off, t)?.contiguous()?;
        let sin = self.sin.narrow(0, off, t)?.contiguous()?;
        let mask = if t > 1 {
            let s = off + t;
            let mask: Vec<f32> = (0..t)
                .flat_map(|i| (0..s).map(move |j| if j > off + i { f32::NEG_INFINITY } else { 0.0 }))
                .collect();
            Some(Tensor::from_vec(mask, (t, s), &self.device)?)
        } else {
            None
        };
        Ok(Frame { cos, sin, mask })
    }

    fn layer(&self, li: usize, x: &Tensor, f: &Frame, cache: Option<&mut KvCache>, fast: bool, grad: bool) -> Result<Tensor> {
        let cfg = &self.cfg;
        let l = &self.layers[li];
        let (t, _) = x.dims2()?;
        let (h, kv, d) = (cfg.num_attention_heads, cfg.kv_heads(), cfg.head_dim());
        let eps = cfg.rms_norm_eps;
        let scale = 1.0 / (d as f64).sqrt();
        let hn = Self::rms(x, &l.ln1, eps, fast, grad)?;
        let heads = |y: Tensor, n: usize| -> Result<Tensor> {
            Ok(y.reshape((1, t, n, d))?.transpose(1, 2)?.contiguous()?)
        };
        let mut q = heads(l.q.forward(&hn, grad)?, h)?;
        let mut k = heads(l.k.forward(&hn, grad)?, kv)?;
        let mut v = heads(l.v.forward(&hn, grad)?, kv)?;
        if let Some(w) = &l.q_norm {
            q = Self::rms(&q, w, eps, fast, grad)?;
        }
        if let Some(w) = &l.k_norm {
            k = Self::rms(&k, w, eps, fast, grad)?;
        }
        if fast {
            q = candle_nn::rotary_emb::rope(&q.contiguous()?, &f.cos, &f.sin)?;
            k = candle_nn::rotary_emb::rope(&k.contiguous()?, &f.cos, &f.sin)?;
        } else {
            q = candle_nn::rotary_emb::rope_slow(&q, &f.cos, &f.sin)?;
            k = candle_nn::rotary_emb::rope_slow(&k, &f.cos, &f.sin)?;
        }
        if let Some(c) = cache {
            if let Some((pk, pv)) = &c.layers[li] {
                k = Tensor::cat(&[pk, &k], 2)?;
                v = Tensor::cat(&[pv, &v], 2)?;
            }
            c.layers[li] = Some((k.clone(), v.clone()));
        }
        let s = k.dim(2)?;
        let rep = h / kv;
        let expand = |y: &Tensor| -> Result<Tensor> {
            if rep == 1 {
                return Ok(y.clone());
            }
            Ok(y.unsqueeze(2)?.expand((1, kv, rep, s, d))?.reshape((1, h, s, d))?)
        };
        let (k, v) = (expand(&k)?, expand(&v)?);
        // Scores in f32 (as SDPA/flash kernels keep them): bf16 scores lose ~0.1 per logit at |s|~20,
        // which made bf16 log-probs ~15x noisier than HF bf16 and flipped greedy tokens.
        let f32 = |y: &Tensor| y.to_dtype(DType::F32);
        let mut att = (f32(&q)?.matmul(&f32(&k)?.t()?)? * scale)?;
        if let Some(mask) = &f.mask {
            att = att.broadcast_add(mask)?;
        }
        let att = if fast {
            candle_nn::ops::softmax_last_dim(&att)?
        } else {
            candle_nn::ops::softmax(&att, D::Minus1)?
        };
        let y = att.to_dtype(self.dtype)?.matmul(&v)?;
        let y = y.transpose(1, 2)?.reshape((t, h * d))?;
        let x = (x + l.o.forward(&y, grad)?)?;
        let hn = Self::rms(&x, &l.ln2, eps, fast, grad)?;
        let g = l.gate.forward(&hn, grad)?.silu()?;
        let u = l.up.forward(&hn, grad)?;
        Ok((x + l.down.forward(&(g * u)?, grad)?)?)
    }

    fn embed(&self, ids: &[u32], grad: bool) -> Result<Tensor> {
        let ids = Tensor::new(ids, &self.device)?;
        Ok(self.embed.get(grad)?.index_select(&ids, 0)?)
    }

    fn head(&self, h: &Tensor, fast: bool, grad: bool) -> Result<Tensor> {
        let h = Self::rms(h, &self.norm, self.cfg.rms_norm_eps, fast, grad)?;
        let w = match &self.lm_head {
            Some(w) => w.get(grad)?,
            None => self.embed.get(grad)?,
        };
        Ok(h.matmul(&w.t()?)?.to_dtype(DType::F32)?)
    }

    fn head_logprobs(&self, h: &Tensor, ids: &[u32], positions: &[usize], grad: bool) -> Result<Tensor> {
        let rows: Vec<u32> = positions.iter().map(|&p| (p - 1) as u32).collect();
        let rows = Tensor::new(rows.as_slice(), &self.device)?;
        let logits = self.head(&h.index_select(&rows, 0)?, false, grad)?;
        let lsm = candle_nn::ops::log_softmax(&logits, D::Minus1)?;
        let tgt: Vec<u32> = positions.iter().map(|&p| ids[p]).collect();
        let tgt = Tensor::new(tgt.as_slice(), &self.device)?.unsqueeze(1)?;
        Ok(lsm.gather(&tgt, 1)?.squeeze(1)?)
    }

    fn check_positions(ids: &[u32], positions: &[usize]) -> Result<usize> {
        if positions.iter().any(|&p| p == 0 || p >= ids.len()) {
            bail!("logprob positions must lie in 1..{}", ids.len());
        }
        positions.iter().max().copied().context("no positions")
    }

    pub fn step(&self, ids: &[u32], cache: &mut KvCache) -> Result<Vec<f32>> {
        if ids.is_empty() {
            bail!("step needs at least one token");
        }
        let off = cache.len();
        let f = self.frame(off, ids.len())?;
        let mut x = self.embed(ids, false)?;
        for li in 0..self.layers.len() {
            x = self.layer(li, &x, &f, Some(cache), true, false)?;
        }
        cache.len = off + ids.len();
        let last = x.i(ids.len() - 1..ids.len())?;
        Ok(self.head(&last, true, false)?.squeeze(0)?.to_vec1()?)
    }

    pub fn logprobs(&self, ids: &[u32], positions: &[usize], grad: bool) -> Result<Tensor> {
        let last = Self::check_positions(ids, positions)?;
        let f = self.frame(0, last)?;
        let mut x = self.embed(&ids[..last], grad)?;
        for li in 0..self.layers.len() {
            x = self.layer(li, &x, &f, None, false, grad)?;
        }
        self.head_logprobs(&x, ids, positions, grad)
    }

    /// Checkpointed by layer: the forward keeps only each layer's input; the backward recomputes
    /// one layer at a time and hands the gradient of its input down to the layer below, so only
    /// one layer's activations are ever alive. One extra forward buys memory that no longer
    /// grows with depth.
    pub fn logprob_grads(&self, ids: &[u32], positions: &[usize], weights: &[f32]) -> Result<(Vec<f32>, Vec<Option<Tensor>>)> {
        let mut acc = vec![None; self.trainable().len()];
        let lp = self.logprob_grads_into(ids, positions, weights, &mut acc)?;
        Ok((lp, acc))
    }

    /// [`Model::logprob_grads`], adding the gradient into `acc` as each layer produces it, so
    /// summing over many sequences holds one set of gradients, not two (30 GB each at 7B, full).
    pub fn logprob_grads_into(&self, ids: &[u32], positions: &[usize], weights: &[f32],
                              acc: &mut [Option<Tensor>]) -> Result<Vec<f32>> {
        if weights.len() != positions.len() {
            bail!("one weight per position");
        }
        let last = Self::check_positions(ids, positions)?;
        let vars = self.trainable();
        if acc.len() != vars.len() {
            bail!("gradient accumulator has {} slots for {} parameters", acc.len(), vars.len());
        }
        let mut absorb = |g: &candle_core::backprop::GradStore| -> Result<()> {
            for (i, (_, v)) in vars.iter().enumerate() {
                if let Some(t) = g.get(v.as_tensor()) {
                    acc[i] = Some(match acc[i].take() {
                        Some(a) => (a + t)?,
                        None => t.clone(),
                    });
                }
            }
            Ok(())
        };
        let f = self.frame(0, last)?;
        let mut xs = vec![self.embed(&ids[..last], false)?];
        for li in 0..self.layers.len() {
            let y = self.layer(li, xs.last().unwrap(), &f, None, false, false)?;
            xs.push(y);
        }
        let top = Var::from_tensor(&xs.pop().unwrap())?;
        let lp = self.head_logprobs(top.as_tensor(), ids, positions, true)?;
        let w = Tensor::new(weights, &self.device)?;
        let g = (&lp * &w)?.sum_all()?.backward()?;
        absorb(&g)?;
        let mut gy = g.get(top.as_tensor()).context("no gradient reached the last layer")?.detach();
        drop(g);
        for li in (0..self.layers.len()).rev() {
            let xv = Var::from_tensor(&xs.pop().unwrap())?;
            let y = self.layer(li, xv.as_tensor(), &f, None, false, true)?;
            let g = (y * &gy)?.sum_all()?.backward()?;
            absorb(&g)?;
            gy = match g.get(xv.as_tensor()) {
                Some(t) => t.detach(),
                None => gy.zeros_like()?,
            };
        }
        if self.embed.master.is_some() {
            let e = self.embed(&ids[..last], true)?;
            absorb(&(e * &gy)?.sum_all()?.backward()?)?;
        }
        Ok(lp.detach().to_vec1()?)
    }

    pub fn trainable(&self) -> Vec<(String, Var)> {
        let mut out = vec![];
        let mut push = |name: String, p: &Param| {
            if let Some(m) = &p.master {
                out.push((name, m.clone()));
            }
        };
        push("model.embed_tokens.weight".into(), &self.embed);
        for (i, l) in self.layers.iter().enumerate() {
            let p = format!("model.layers.{i}");
            push(format!("{p}.input_layernorm.weight"), &l.ln1);
            push(format!("{p}.post_attention_layernorm.weight"), &l.ln2);
            if let Some(w) = &l.q_norm {
                push(format!("{p}.self_attn.q_norm.weight"), w);
            }
            if let Some(w) = &l.k_norm {
                push(format!("{p}.self_attn.k_norm.weight"), w);
            }
            for lin in [&l.q, &l.k, &l.v, &l.o, &l.gate, &l.up, &l.down] {
                push(format!("{}.weight", lin.name), &lin.w);
                if let Some(b) = &lin.b {
                    push(format!("{}.bias", lin.name), b);
                }
                if let Some(lo) = &lin.lora {
                    push(format!("{}.lora_A.weight", lin.name), &lo.a);
                    push(format!("{}.lora_B.weight", lin.name), &lo.b);
                }
            }
        }
        push("model.norm.weight".into(), &self.norm);
        if let Some(w) = &self.lm_head {
            push("lm_head.weight".into(), w);
        }
        out
    }

    pub fn refresh(&mut self) -> Result<()> {
        self.embed.refresh()?;
        self.norm.refresh()?;
        if let Some(w) = &mut self.lm_head {
            w.refresh()?;
        }
        for l in &mut self.layers {
            for p in [&mut l.ln1, &mut l.ln2] {
                p.refresh()?;
            }
            for p in [&mut l.q_norm, &mut l.k_norm].into_iter().flatten() {
                p.refresh()?;
            }
            for lin in [&mut l.q, &mut l.k, &mut l.v, &mut l.o, &mut l.gate, &mut l.up, &mut l.down] {
                lin.w.refresh()?;
                if let Some(b) = &mut lin.b {
                    b.refresh()?;
                }
                if let Some(lo) = &mut lin.lora {
                    lo.a.refresh()?;
                    lo.b.refresh()?;
                }
            }
        }
        Ok(())
    }

    pub fn load_trainable(&mut self, values: &HashMap<String, Tensor>) -> Result<()> {
        for (name, var) in self.trainable() {
            let t = values.get(&name).with_context(|| format!("checkpoint lacks {name}"))?;
            if t.dims() != var.dims() {
                bail!("checkpoint {name} has shape {:?}, model wants {:?}", t.dims(), var.dims());
            }
            var.set(&t.to_dtype(DType::F32)?.to_device(&self.device)?)?;
        }
        self.refresh()
    }

    pub fn lora_info(&self) -> Option<(usize, f64, Vec<String>)> {
        match &self.adapt {
            Adapt::Lora { rank, alpha, .. } => Some((*rank, *alpha, self.adapt.lora_targets())),
            Adapt::Full => None,
        }
    }

    pub fn export_weights(&self) -> Vec<(String, Tensor)> {
        let mut out = vec![("model.embed_tokens.weight".to_string(), self.embed.compute.clone())];
        for (i, l) in self.layers.iter().enumerate() {
            let p = format!("model.layers.{i}");
            out.push((format!("{p}.input_layernorm.weight"), l.ln1.compute.clone()));
            out.push((format!("{p}.post_attention_layernorm.weight"), l.ln2.compute.clone()));
            if let Some(w) = &l.q_norm {
                out.push((format!("{p}.self_attn.q_norm.weight"), w.compute.clone()));
            }
            if let Some(w) = &l.k_norm {
                out.push((format!("{p}.self_attn.k_norm.weight"), w.compute.clone()));
            }
            for lin in [&l.q, &l.k, &l.v, &l.o, &l.gate, &l.up, &l.down] {
                out.push((format!("{}.weight", lin.name), lin.w.compute.clone()));
                if let Some(b) = &lin.b {
                    out.push((format!("{}.bias", lin.name), b.compute.clone()));
                }
            }
        }
        out.push(("model.norm.weight".into(), self.norm.compute.clone()));
        if let Some(w) = &self.lm_head {
            out.push(("lm_head.weight".into(), w.compute.clone()));
        }
        out
    }
}
