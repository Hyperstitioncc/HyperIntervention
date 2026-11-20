use crate::rollout::{Decider, Decision, Judge, RolloutView, StepView, Verdict};
use anyhow::Result;

pub struct AcceptAll;

impl Decider for AcceptAll {
    fn decide(&mut self, _: &StepView) -> Result<Decision> {
        Ok(Decision::Accept)
    }
}

pub struct AcceptAtEnd;

impl Judge for AcceptAtEnd {
    fn judge(&mut self, _: &RolloutView) -> Result<Verdict> {
        Ok(Verdict::Done)
    }
}

pub struct DecideFn<F>(pub F);

impl<F: FnMut(&StepView) -> Result<Decision>> Decider for DecideFn<F> {
    fn decide(&mut self, view: &StepView) -> Result<Decision> {
        (self.0)(view)
    }
}

pub struct JudgeFn<F>(pub F);

impl<F: FnMut(&RolloutView) -> Result<Verdict>> Judge for JudgeFn<F> {
    fn judge(&mut self, view: &RolloutView) -> Result<Verdict> {
        (self.0)(view)
    }
}

pub struct FirstOf(pub Vec<Box<dyn Decider>>);

impl Decider for FirstOf {
    fn decide(&mut self, view: &StepView) -> Result<Decision> {
        for d in self.0.iter_mut() {
            let out = d.decide(view)?;
            if out != Decision::Accept {
                return Ok(out);
            }
        }
        Ok(Decision::Accept)
    }
}

pub struct AllOf(pub Vec<Box<dyn Judge>>);

impl Judge for AllOf {
    fn judge(&mut self, view: &RolloutView) -> Result<Verdict> {
        for j in self.0.iter_mut() {
            let out = j.judge(view)?;
            if out != Verdict::Done {
                return Ok(out);
            }
        }
        Ok(Verdict::Done)
    }
}

pub struct Gate<F, D> {
    pub when: F,
    pub inner: D,
}

impl<F: FnMut(&StepView) -> bool, D: Decider> Decider for Gate<F, D> {
    fn decide(&mut self, view: &StepView) -> Result<Decision> {
        if (self.when)(view) {
            self.inner.decide(view)
        } else {
            Ok(Decision::Accept)
        }
    }
}

pub struct Script(pub Vec<(usize, Decision)>);

impl Decider for Script {
    fn decide(&mut self, view: &StepView) -> Result<Decision> {
        if let Some(i) = self.0.iter().position(|(p, _)| *p == view.position) {
            return Ok(self.0.remove(i).1);
        }
        Ok(Decision::Accept)
    }
}

pub struct ScriptJudge(pub Vec<Verdict>);

impl Judge for ScriptJudge {
    fn judge(&mut self, _: &RolloutView) -> Result<Verdict> {
        if self.0.is_empty() {
            Ok(Verdict::Done)
        } else {
            Ok(self.0.remove(0))
        }
    }
}
