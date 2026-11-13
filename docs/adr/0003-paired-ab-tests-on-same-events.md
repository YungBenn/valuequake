# A/B tests are paired: every arm scores the same Events

Live post-cutoff Events are scarce (a few hundred over months; see ADR 0001), so splitting them between arms would halve the sample and add Event-to-Event noise. Each A/B arm (AMT context on/off, Analogs on/off, prompt versions) scores the same Event, and the arms are compared by RPS on identical rows. One arm is marked primary for the UI. This roughly doubles Opus spend on the Events under test, which is accepted and capped by the monthly budget.
