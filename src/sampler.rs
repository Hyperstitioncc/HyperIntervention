use rand::Rng;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SamplingConfig {
    pub temperature: f32,
    pub top_p: f32,
    pub top_k: usize,
    pub max_new_tokens: usize,
    pub show_top: usize,
}

impl Default for SamplingConfig {
    fn default() -> Self {
        Self { temperature: 1.0, top_p: 1.0, top_k: 0, max_new_tokens: 1024, show_top: 20 }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Candidate {
    pub token: u32,
    pub prob: f32,
}

#[derive(Debug, Clone)]
pub struct Proposal {
    pub token: u32,
    pub prob: f32,
    pub top: Vec<Candidate>,
    pub probs: Vec<f32>,
    pub entropy: f32,
}

fn softmax(logits: &[f32], temperature: f32) -> Vec<f32> {
    let max = logits.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
    let mut p: Vec<f32> = logits.iter().map(|&l| ((l - max) / temperature).exp()).collect();
    let sum: f32 = p.iter().sum();
    for x in p.iter_mut() {
        *x /= sum;
    }
    p
}

fn top_indices(p: &[f32], k: usize) -> Vec<usize> {
    let k = k.min(p.len());
    let mut idx: Vec<usize> = (0..p.len()).collect();
    if k == 0 {
        return vec![];
    }
    if k < idx.len() {
        idx.select_nth_unstable_by(k - 1, |&a, &b| p[b].total_cmp(&p[a]));
        idx.truncate(k);
    }
    idx.sort_by(|&a, &b| p[b].total_cmp(&p[a]));
    idx
}

pub fn propose(logits: &[f32], cfg: &SamplingConfig, rng: &mut impl Rng) -> Proposal {
    let probs = softmax(logits, 1.0);
    let token = if cfg.temperature <= 0.0 {
        top_indices(&probs, 1)[0]
    } else {
        let q = if cfg.temperature == 1.0 { probs.clone() } else { softmax(logits, cfg.temperature) };
        let truncate = cfg.top_k > 0 || cfg.top_p < 1.0;
        let pool: Vec<usize> = if truncate {
            let k = if cfg.top_k > 0 { cfg.top_k } else { q.len() };
            let mut keep = top_indices(&q, k);
            if cfg.top_p < 1.0 {
                let mut mass = 0.0;
                let mut n = 0;
                for &i in &keep {
                    mass += q[i];
                    n += 1;
                    if mass >= cfg.top_p {
                        break;
                    }
                }
                keep.truncate(n.max(1));
            }
            keep
        } else {
            (0..q.len()).collect()
        };
        let total: f32 = pool.iter().map(|&i| q[i]).sum();
        let mut r = rng.gen::<f32>() * total;
        let mut pick = *pool.last().unwrap();
        for &i in &pool {
            r -= q[i];
            if r <= 0.0 {
                pick = i;
                break;
            }
        }
        pick
    };
    let top = top_indices(&probs, cfg.show_top)
        .into_iter()
        .map(|i| Candidate { token: i as u32, prob: probs[i] })
        .collect();
    let entropy = -probs.iter().filter(|&&p| p > 0.0).map(|&p| p * p.ln()).sum::<f32>();
    Proposal { token: token as u32, prob: probs[token], top, probs, entropy }
}
