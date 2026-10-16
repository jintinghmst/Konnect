# Trace, Via, and Impedance Sizing

This reference defines the sizing process, not universal dimensions. Store the
accepted results in project netclasses and predefined sizes so routing tools use
the same values that were reviewed.

## Current-carrying traces

For each current-carrying net, capture:

- continuous and transient current;
- copper thickness and plating assumptions;
- external or internal layer;
- ambient and temperature-rise budget;
- trace length and voltage-drop budget;
- available routing width and thermal environment; and
- the selected fabricator's current minimums and stackup.

Use an accepted current-capacity method or calculator with those inputs. Record
the method, inputs, result, and chosen margin. Changing copper weight, layer,
temperature, length, or allowed drop requires a new calculation; a scale factor
is not sufficient acceptance evidence.

Completion criterion: the selected width satisfies both thermal and voltage-drop
limits and is no narrower than the current fabrication contract.

## Ordinary signals

For an ordinary, non-impedance-controlled signal, choose a width and clearance
that the selected process can fabricate reliably and the available geometry can
route. Keep one project netclass as the source of truth. A prose default is only
a candidate until it is written to the project and passes DRC.

## Controlled impedance

Which nets need controlled impedance, and how to route them, is covered in
[`high-speed-design.md`](high-speed-design.md): decide by edge rate, fix the
stackup and reference planes at architecture stage, and verify against a
channel budget.

To size the geometry, read the live stackup with `get_board_stackup`. An
external microstrip and an internal stripline have different fields; an
internal conductor is not a microstrip. Solve width (and pair gap) with a
field solver or the fabricator's stackup calculator, record the target
impedance and tolerance, layer, reference plane, solver and result, then write
the values into a project netclass. A changed laminate, stackup or fabricator is a
technical change: re-solve and re-verify.

## Vias

Choose via drill and finished diameter from the selected fabricator's current
capability, required annular ring, board thickness/aspect ratio, current, and
reliability target. Power and thermal paths may require parallel vias; justify
their count with electrical/thermal evidence rather than a fixed lookup table.

Use `set_predefined_sizes` to record accepted via choices and
`get_predefined_sizes` to verify the stored palette before routing.

## Acceptance record

For every non-default netclass, preserve the sizing purpose, governing inputs,
calculation or current contract, selected values, and DRC result. A required
input that is unavailable makes the sizing decision `INCOMPLETE`.
