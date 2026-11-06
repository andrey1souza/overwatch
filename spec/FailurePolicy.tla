------------------------- MODULE FailurePolicy -------------------------
(* Section: Imports

Shared operators used by this module are imported below.
*)
EXTENDS FiniteSets

(* Section: Failure policy

A timeout or a finding outside the inherited baseline is a new failure.
The inherited policy determines whether existing findings also guard an action.
*)
NewFailure(failures, baseline) ==
    "timeout" \in failures \/ ~(failures \subseteq baseline)

FailureGuards(failures, baseline, inherited) ==
    failures # {} /\ (inherited = "block" \/ NewFailure(failures, baseline))
=============================================================================
