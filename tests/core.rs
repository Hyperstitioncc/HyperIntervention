use candle_core::{DType, Device, Tensor};
use manual_rl::compose::{AcceptAll, AcceptAtEnd, DecideFn, Script, ScriptJudge};
use manual_rl::config::ModelConfig;
use manual_rl::model::{Adapt, KvCache, Model};
use manual_rl::rollout::{run_episode, Decision, Episode, EpisodeConfig, PromptItem, Text, Verdict};
use manual_rl::sampler::SamplingConfig;
use manual_rl::train::{update, AdamW, UpdateConfig};
use rand::{Rng, SeedableRng};
use std::collections::HashMap;

const V: usize = 64;

fn tiny_cfg(qwen3: bool) -> ModelConfig {
    serde_json::from_value(serde_json::json!({
        "model_type": if qwen3 { "qwen3" } else { "qwen2" },
        "vocab_size": V, "hidden_size": 32, "intermediate_size": 64, "num_hidden_layers": 2,
        "num_attention_heads": 4, "num_key_value_heads": 2, "head_dim": 8,
        "rms_norm_eps": 1e-6, "rope_theta": 10000.0, "tie_word_embeddings": false
    }))
    .unwrap()
}

fn tiny_weights(cfg: &ModelConfig, qwen3: bool) -> HashMap<String, Tensor> {
    let mut rng = rand::rngs::StdRng::seed_from_u64(7);
    let mut w = HashMap::new();
    let mut put = |name: String, shape: &[usize], scale: f32, offset: f32| {
        let n: usize = shape.iter().product();
        let v: Vec<f32> = (0..n).map(|_| offset + scale * (rng.gen::<f32>() * 2.0 - 1.0)).collect();
        w.insert(name, Tensor::from_vec(v, shape, &Device::Cpu).unwrap());
    };
    let (h, i, d) = (cfg.hidden_size, cfg.intermediate_size, cfg.head_dim());
    let (nh, kv) = (cfg.num_attention_heads, cfg.kv_heads());
    put("model.embed_tokens.weight".into(), &[V, h], 1.0, 0.0);
    put("lm_head.weight".into(), &[V, h], 0.5, 0.0);
    put("model.norm.weight".into(), &[h], 0.1, 1.0);
    for l in 0..cfg.num_hidden_layers {
        let p = format!("model.layers.{l}");
        put(format!("{p}.input_layernorm.weight"), &[h], 0.1, 1.0);
        put(format!("{p}.post_attention_layernorm.weight"), &[h], 0.1, 1.0);
        put(format!("{p}.self_attn.q_proj.weight"), &[nh * d, h], 0.3, 0.0);
        put(format!("{p}.self_attn.k_proj.weight"), &[kv * d, h], 0.3, 0.0);
        put(format!("{p}.self_attn.v_proj.weight"), &[kv * d, h], 0.3, 0.0);
        put(format!("{p}.self_attn.o_proj.weight"), &[h, nh * d], 0.3, 0.0);
        if qwen3 {
            put(format!("{p}.self_attn.q_norm.weight"), &[d], 0.1, 1.0);
            put(format!("{p}.self_attn.k_norm.weight"), &[d], 0.1, 1.0);
        } else {
            put(format!("{p}.self_attn.q_proj.bias"), &[nh * d], 0.1, 0.0);
            put(format!("{p}.self_attn.k_proj.bias"), &[kv * d], 0.1, 0.0);
            put(format!("{p}.self_attn.v_proj.bias"), &[kv * d], 0.1, 0.0);
        }
        put(format!("{p}.mlp.gate_proj.weight"), &[i, h], 0.3, 0.0);
        put(format!("{p}.mlp.up_proj.weight"), &[i, h], 0.3, 0.0);
        put(format!("{p}.mlp.down_proj.weight"), &[h, i], 0.3, 0.0);
    }
    w
}

fn tiny(adapt: Adapt, qwen3: bool) -> Model {
    let cfg = tiny_cfg(qwen3);
    let w = tiny_weights(&cfg, qwen3);
    Model::build(cfg, &w, adapt, DType::F32, &Device::Cpu, 256, 1).unwrap()
}

fn lora() -> Adapt {
    Adapt::Lora { rank: 4, alpha: 8.0, targets: vec![] }
}

fn prompt() -> PromptItem {
    PromptItem { id: "p".into(), prompt: "".into(), extra: serde_json::Value::Null }
}

fn ecfg(n: usize) -> EpisodeConfig {
    EpisodeConfig {
        sampling: SamplingConfig { temperature: 1.0, max_new_tokens: n, ..Default::default() },
        max_revisions: 4,
        eos: vec![],
    }
}

fn episode(model: &Model, d: &mut dyn manual_rl::rollout::Decider, j: &mut dyn manual_rl::rollout::Judge, n: usize) -> Episode {
    let mut rng = rand::rngs::StdRng::seed_from_u64(3);
    run_episode(model, Text(None), &prompt(), &[1, 2, 3], d, j, &ecfg(n), &mut rng).unwrap()
}

#[test]
fn cache_matches_full_sequence() {
    for qwen3 in [false, true] {
        let m = tiny(lora(), qwen3);
        let ids: Vec<u32> = vec![5, 9, 1, 33, 7, 2, 60, 11, 4];
        let full: Vec<f32> = m.logprobs(&ids, &(1..ids.len()).collect::<Vec<_>>(), false).unwrap().to_vec1().unwrap();
        let mut cache = KvCache::new(m.n_layers());
        let mut logits = m.step(&ids[..1], &mut cache).unwrap();
        for (k, &t) in ids.iter().enumerate().skip(1) {
            let lse = logits.iter().map(|x| x.exp()).sum::<f32>().ln();
            assert!((logits[t as usize] - lse - full[k - 1]).abs() < 1e-4, "qwen3={qwen3} pos {k}");
            if k == 5 {
                cache.truncate(3).unwrap();
                logits = m.step(&ids[3..5], &mut cache).unwrap();
                let lse = logits.iter().map(|x| x.exp()).sum::<f32>().ln();
                assert!((logits[t as usize] - lse - full[k - 1]).abs() < 1e-4);
            }
            logits = m.step(&[t], &mut cache).unwrap();
        }
    }
}

#[test]
fn accepting_everything_records_no_forks() {
    let m = tiny(lora(), false);
    let ep = episode(&m, &mut AcceptAll, &mut AcceptAtEnd, 12);
    assert_eq!(ep.completion.len(), 12);
    assert!(ep.forks.is_empty());
}

#[test]
fn forcing_records_a_fork_unless_it_matches_the_model() {
    let m = tiny(lora(), false);
    let mut d = DecideFn(|v: &manual_rl::rollout::StepView| {
        Ok(match v.position {
            4 => Decision::Force(vec![(v.proposal.token + 1) % V as u32, 17]),
            8 => Decision::Force(vec![v.proposal.token]),
            _ => Decision::Accept,
        })
    });
    let ep = episode(&m, &mut d, &mut AcceptAtEnd, 12);
    assert_eq!(ep.forks.len(), 1);
    let f = &ep.forks[0];
    assert_eq!(f.pos, 4);
    assert_eq!(f.forced.len(), 2);
    assert_eq!(f.forced[1], 17);
    assert_eq!(f.rejected.len(), 1);
    assert_eq!(ep.completion[4..6], f.forced[..]);
    assert_eq!(ep.forced_tokens, 3);
    ep.check().unwrap();
}

#[test]
fn rewind_makes_the_discarded_run_the_rejected_side() {
    let m = tiny(lora(), false);
    let mut seen: Vec<u32> = vec![];
    let mut d = DecideFn(move |v: &manual_rl::rollout::StepView| {
        if v.position == 2 && v.forks.is_empty() && seen.is_empty() {
            seen.push(1);
            return Ok(Decision::Force(vec![(v.proposal.token + 1) % V as u32]));
        }
        if v.position == 9 && seen.len() == 1 {
            seen.push(v.completion[1]);
            let t = (v.completion[1] + 1) % V as u32;
            return Ok(Decision::Rewind { to: 1, force: vec![t, t] });
        }
        Ok(Decision::Accept)
    });
    let ep = episode(&m, &mut d, &mut AcceptAtEnd, 12);
    assert_eq!(ep.forks.len(), 1, "the fork at 2 lay past the rewind and is gone");
    let f = &ep.forks[0];
    assert_eq!(f.pos, 1);
    assert_eq!(f.rejected.len(), 8, "positions 1..9 were discarded");
    assert_eq!(f.forced.len(), 2);
    ep.check().unwrap();
}

#[test]
fn a_judge_can_send_the_rollout_back() {
    let m = tiny(lora(), false);
    let mut j = ScriptJudge(vec![Verdict::Revise { to: 5, force: vec![63] }]);
    let ep = episode(&m, &mut AcceptAll, &mut j, 10);
    assert_eq!(ep.revisions, 1);
    assert_eq!(ep.completion.len(), 10);
    assert_eq!(ep.completion[5], 63);
    if let Some(f) = ep.forks.first() {
        assert_eq!(f.pos, 5);
        assert_eq!(f.rejected.len(), 5);
    }
    ep.check().unwrap();
}

#[test]
fn script_decider_replays_by_position() {
    let m = tiny(lora(), false);
    let mut d = Script(vec![(3, Decision::Force(vec![40])), (6, Decision::Stop)]);
    let ep = episode(&m, &mut d, &mut AcceptAtEnd, 20);
    assert_eq!(ep.completion.len(), 6);
    assert_eq!(ep.completion[3], 40);
}

fn steered_episodes(m: &Model, n: usize) -> Vec<Episode> {
    (0..n)
        .map(|k| {
            let mut d = DecideFn(move |v: &manual_rl::rollout::StepView| {
                Ok(if v.position == 2 + k % 3 { Decision::Force(vec![(v.proposal.token + 7) % V as u32]) } else { Decision::Accept })
            });
            episode(m, &mut d, &mut AcceptAtEnd, 8)
        })
        .collect()
}

fn margins(m: &Model, eps: &[Episode]) -> f64 {
    let mut s = 0.0;
    for e in eps {
        for f in &e.forks {
            let mut c = e.prompt_tokens.clone();
            c.extend(&e.completion[..f.pos + 1]);
            let mut r = e.prompt_tokens.clone();
            r.extend(&e.completion[..f.pos]);
            r.push(f.rejected[0]);
            let pc: f32 = m.logprobs(&c, &[c.len() - 1], false).unwrap().to_vec1().unwrap()[0];
            let pr: f32 = m.logprobs(&r, &[r.len() - 1], false).unwrap().to_vec1().unwrap()[0];
            s += (pc - pr) as f64;
        }
    }
    s
}

fn trains(adapt: Adapt) {
    let mut m = tiny(adapt, true);
    let eps = steered_episodes(&m, 4);
    let base_head: Vec<f32> = m.export_weights().into_iter().find(|(n, _)| n == "lm_head.weight").unwrap().1.flatten_all().unwrap().to_vec1().unwrap();
    let mut opt = AdamW::new(&m.trainable()).unwrap();
    let cfg = UpdateConfig { lr: Some(1e-2), beta: 0.5, minibatch: 2, epochs: 2, pair_horizon: 1, ..Default::default() };
    let before = margins(&m, &eps);
    let mut last = None;
    for _ in 0..3 {
        last = Some(update(&mut m, &mut opt, &eps, &cfg).unwrap());
    }
    let st = last.unwrap();
    let after = margins(&m, &eps);
    assert_eq!(st.pairs, 4);
    assert!(after > before + 1.0, "margin {before} -> {after}");
    assert!(st.margin_after > st.margin_before);
    let head: Vec<f32> = m.export_weights().into_iter().find(|(n, _)| n == "lm_head.weight").unwrap().1.flatten_all().unwrap().to_vec1().unwrap();
    let moved = head.iter().zip(&base_head).any(|(a, b)| a != b);
    assert_eq!(moved, m.lora_info().is_none(), "LoRA leaves base weights alone; full moves them");
}

#[test]
fn lora_update_prefers_the_forced_tokens() {
    trains(lora());
}

#[test]
fn full_update_prefers_the_forced_tokens() {
    trains(Adapt::Full);
}

#[test]
fn sft_only_raises_the_accepted_rollouts() {
    let mut m = tiny(lora(), false);
    let eps = steered_episodes(&m, 3);
    let mut opt = AdamW::new(&m.trainable()).unwrap();
    let cfg = UpdateConfig { lr: Some(1e-2), pair_weight: 0.0, sft_weight: 1.0, epochs: 3, ..Default::default() };
    let st = update(&mut m, &mut opt, &eps, &cfg).unwrap();
    assert!(st.sft_logp_after > st.sft_logp_before, "{st:?}");
}

#[test]
fn forced_only_raises_the_forced_tokens() {
    for adapt in [lora(), Adapt::Full] {
        let mut m = tiny(adapt, true);
        let eps = steered_episodes(&m, 4);
        let mut opt = AdamW::new(&m.trainable()).unwrap();
        let cfg = UpdateConfig { lr: Some(1e-2), pair_weight: 0.0, forced_weight: 1.0, minibatch: 2, epochs: 3, ..Default::default() };
        let st = update(&mut m, &mut opt, &eps, &cfg).unwrap();
        assert_eq!(st.pairs, 4);
        assert!(st.forced_logp_after > st.forced_logp_before + 0.5, "{st:?}");
    }
}

#[test]
fn checkpoint_round_trip() {
    let dir = std::env::temp_dir().join(format!("mrl-ckpt-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let model_dir = dir.join("model");
    std::fs::create_dir_all(&model_dir).unwrap();
    let mut m = tiny(lora(), false);
    let eps = steered_episodes(&m, 2);
    let mut opt = AdamW::new(&m.trainable()).unwrap();
    let cfg = UpdateConfig { lr: Some(1e-2), ..Default::default() };
    update(&mut m, &mut opt, &eps, &cfg).unwrap();
    let state = manual_rl::checkpoint::RunState { round: 1, prompts_used: 2, adam_t: 0 };
    let ck = manual_rl::checkpoint::save(&dir, &m, &opt, &state, &model_dir, 2).unwrap();
    assert!(ck.join("adapter_model.safetensors").exists());
    let mut m2 = tiny(lora(), false);
    let mut opt2 = AdamW::new(&m2.trainable()).unwrap();
    let st = manual_rl::checkpoint::resume(&dir, &mut m2, &mut opt2).unwrap().unwrap();
    assert_eq!(st.round, 1);
    assert_eq!(opt2.t, opt.t);
    let ids = [1u32, 2, 3, 4, 5];
    let a: Vec<f32> = m.logprobs(&ids, &[1, 2, 3, 4], false).unwrap().to_vec1().unwrap();
    let b: Vec<f32> = m2.logprobs(&ids, &[1, 2, 3, 4], false).unwrap().to_vec1().unwrap();
    assert_eq!(a, b);
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn external_process_serves_decider_and_judge() {
    let script = r#"
import json, sys
judged = 0
for line in sys.stdin:
    r = json.loads(line)
    if r["type"] == "step":
        out = {"action": "force", "tokens": [9]} if r["position"] == 1 else {"action": "accept"}
    else:
        judged += 1
        out = {"verdict": "revise", "to": 3, "tokens": [11]} if judged == 1 else {"verdict": "done"}
    print(json.dumps(out), flush=True)
"#;
    let m = tiny(lora(), false);
    let ext = manual_rl::external::External::spawn(&["python3".into(), "-c".into(), script.into()]).unwrap();
    let (mut d, mut j) = (ext.clone(), ext);
    let ep = episode(&m, &mut d, &mut j, 6);
    assert_eq!(ep.completion[1], 9);
    assert_eq!(ep.completion[3], 11);
    assert_eq!(ep.revisions, 1);
    ep.check().unwrap();
}

#[test]
fn checkpointed_gradients_match_autograd() {
    for adapt in [lora(), Adapt::Full] {
        for qwen3 in [false, true] {
            let m = tiny(adapt.clone(), qwen3);
            let (vars, ids) = (m.trainable(), vec![3u32, 14, 15, 9, 26, 5, 35, 8, 9, 7]);
            let pos = vec![2usize, 5, 6, 9];
            let w = vec![0.5f32, -1.0, 2.0, 0.25];
            for (n, v) in &vars {
                if n.ends_with("lora_B.weight") {
                    v.set(&(v.as_detached_tensor().ones_like().unwrap() * 0.01).unwrap()).unwrap();
                }
            }
            let mut m = m;
            m.refresh().unwrap();
            let lp = m.logprobs(&ids, &pos, true).unwrap();
            let wt = Tensor::new(w.as_slice(), &Device::Cpu).unwrap();
            let full = (&lp * &wt).unwrap().sum_all().unwrap().backward().unwrap();
            let (vals, grads) = m.logprob_grads(&ids, &pos, &w).unwrap();
            assert_eq!(vals, lp.to_vec1::<f32>().unwrap());
            let mut checked = 0;
            for (i, (n, v)) in m.trainable().iter().enumerate() {
                let a = full.get(v.as_tensor()).map(|t| t.flatten_all().unwrap().to_vec1::<f32>().unwrap());
                let b = grads[i].as_ref().map(|t| t.flatten_all().unwrap().to_vec1::<f32>().unwrap());
                match (a, b) {
                    (Some(a), Some(b)) => {
                        let err = a.iter().zip(&b).map(|(x, y)| (x - y).abs()).fold(0f32, f32::max);
                        let scale = a.iter().map(|x| x.abs()).fold(0f32, f32::max).max(1e-6);
                        assert!(err / scale < 1e-4, "{n}: rel err {}", err / scale);
                        checked += 1;
                    }
                    (None, None) => {}
                    (a, b) => panic!("{n}: autograd {} vs checkpointed {}", a.is_some(), b.is_some()),
                }
            }
            assert!(checked > 10, "{checked} parameters compared");
        }
    }
}

#[test]
fn fused_cpu_adam_matches_tensor_ops() {
    use candle_core::{Device, Tensor};
    let dev = Device::Cpu;
    let shape = (37, 1001);
    let m = Tensor::randn(0f32, 0.01, shape, &dev).unwrap();
    let v = Tensor::randn(0f32, 0.01, shape, &dev).unwrap().sqr().unwrap();
    let g = Tensor::randn(0f32, 1.0, shape, &dev).unwrap();
    let (b1, b2, eps, t) = (0.9f64, 0.999f64, 1e-8f64, 3);
    let (bc1, bc2) = (1.0 - b1.powi(t), 1.0 - b2.powi(t));
    let (fm, fv, fu) = manual_rl::train::adam_cpu(&m, &v, &g, b1, b2, bc1, bc2, eps).unwrap();
    let em = ((&m * b1).unwrap() + (&g * (1.0 - b1)).unwrap()).unwrap();
    let ev = ((&v * b2).unwrap() + (g.sqr().unwrap() * (1.0 - b2)).unwrap()).unwrap();
    let eu = (&em / bc1).unwrap().div(&((&ev / bc2).unwrap().sqrt().unwrap() + eps).unwrap()).unwrap();
    for (a, b) in [(fm, em), (fv, ev), (fu, eu)] {
        let d: f32 = (a - b).unwrap().abs().unwrap().max_all().unwrap().to_scalar().unwrap();
        assert_eq!(d, 0.0, "fused CPU AdamW differs from the tensor-op formula");
    }
}
