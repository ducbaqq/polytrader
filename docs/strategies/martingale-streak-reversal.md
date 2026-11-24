# martingale-streak-reversal

**Family:** martingale | **Status:** abandoned
**Current version:** none (never implemented as a `polydata.bt` strategy)

## Hypothesis
After a streak of identical outcomes in a series (several Down windows in a row), bet on the reversal
and double the stake after every loss, so that one win recovers all earlier losses plus a unit profit.

## Variants tried
Count: 1 informal simulation (first session, before the backtester existed), with no saved run folder.

## Results
Rejected by simulation in the first session. Consecutive outcomes are close to independent, so a streak
does not predict the next window: the bet has no edge, and the stake escalation only changes the
shape of the losses. Because the stake doubles after each loss, a long enough losing run exhausts the
capital (ruin risk), and fees make the expected value per bet slightly negative.

## Lessons
- Escalating stakes is not an edge; it moves risk from many small losses to a rare ruinous one.
- Before testing a streak idea, check that outcomes in a series are not independent (the calibration
  study already shows prices are close to calibrated).

## Next
Nothing planned. Revisit only if the outcome series shows measurable serial dependence.
