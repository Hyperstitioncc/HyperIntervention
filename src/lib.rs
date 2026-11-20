pub mod checkpoint;
pub mod compose;
pub mod config;
pub mod external;
pub mod model;
pub mod rollout;
pub mod run;
pub mod sampler;
pub mod serve;
pub mod train;

pub fn phase(label: &str, dev: &candle_core::Device, t0: &mut std::time::Instant) {
    if std::env::var_os("MANUAL_RL_TIMING").is_some() {
        let _ = dev.synchronize();
        eprintln!("[time] {label}: {:.2} s", t0.elapsed().as_secs_f64());
    }
    *t0 = std::time::Instant::now();
}
