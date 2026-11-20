use crate::rollout::{Decider, Decision, Judge, RolloutView, StepView, Text, Verdict};
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::json;
use std::cell::RefCell;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::rc::Rc;

pub struct Process {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Process {
    pub fn spawn(argv: &[String]) -> Result<Self> {
        let (cmd, args) = argv.split_first().context("empty external command")?;
        let mut child = Command::new(cmd)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .with_context(|| format!("spawning {argv:?}"))?;
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Ok(Self { child, stdin, stdout })
    }

    fn ask(&mut self, req: &serde_json::Value) -> Result<Reply> {
        writeln!(self.stdin, "{req}")?;
        self.stdin.flush()?;
        let mut line = String::new();
        if self.stdout.read_line(&mut line)? == 0 {
            bail!("external process closed its stdout");
        }
        serde_json::from_str(line.trim()).with_context(|| format!("bad reply {line:?}"))
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[derive(Deserialize)]
struct Reply {
    action: Option<String>,
    verdict: Option<String>,
    tokens: Option<Vec<u32>>,
    text: Option<String>,
    to: Option<usize>,
}

impl Reply {
    fn tokens(&self, text: Text) -> Result<Vec<u32>> {
        match (&self.tokens, &self.text) {
            (Some(t), _) => Ok(t.clone()),
            (None, Some(s)) => text.encode(s),
            (None, None) => Ok(vec![]),
        }
    }
}

#[derive(Clone)]
pub struct External(Rc<RefCell<Process>>);

impl External {
    pub fn spawn(argv: &[String]) -> Result<Self> {
        Ok(Self(Rc::new(RefCell::new(Process::spawn(argv)?))))
    }
}

fn cand(text: Text, token: u32, prob: f32) -> serde_json::Value {
    json!({"token": token, "prob": prob, "text": text.decode(&[token])})
}

impl Decider for External {
    fn decide(&mut self, v: &StepView) -> Result<Decision> {
        let req = json!({
            "type": "step",
            "prompt": v.prompt,
            "position": v.position,
            "completion": v.completion,
            "text": v.completion_text(),
            "proposal": cand(v.text, v.proposal.token, v.proposal.prob),
            "top": v.top.iter().map(|c| cand(v.text, c.token, c.prob)).collect::<Vec<_>>(),
            "forks": v.forks,
            "revisions": v.revisions,
        });
        let r = self.0.borrow_mut().ask(&req)?;
        match r.action.as_deref() {
            Some("accept") => Ok(Decision::Accept),
            Some("stop") => Ok(Decision::Stop),
            Some("force") => {
                let t = r.tokens(v.text)?;
                if t.is_empty() {
                    bail!("force reply carries no tokens");
                }
                Ok(Decision::Force(t))
            }
            Some("rewind") => Ok(Decision::Rewind {
                to: r.to.context("rewind reply needs \"to\"")?,
                force: r.tokens(v.text)?,
            }),
            other => bail!("unknown decider action {other:?}"),
        }
    }
}

impl Judge for External {
    fn judge(&mut self, v: &RolloutView) -> Result<Verdict> {
        let req = json!({
            "type": "judge",
            "prompt": v.prompt,
            "completion": v.completion,
            "text": v.completion_text(),
            "ended_by": v.ended_by,
            "forks": v.forks,
            "revisions": v.revisions,
        });
        let r = self.0.borrow_mut().ask(&req)?;
        match r.verdict.as_deref() {
            Some("done") => Ok(Verdict::Done),
            Some("abandon") => Ok(Verdict::Abandon),
            Some("revise") => Ok(Verdict::Revise {
                to: r.to.context("revise reply needs \"to\"")?,
                force: r.tokens(v.text)?,
            }),
            other => bail!("unknown judge verdict {other:?}"),
        }
    }
}
