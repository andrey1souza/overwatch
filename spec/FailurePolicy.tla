------------------------- MODULE FailurePolicy -------------------------
EXTENDS FiniteSets

NewFailure(failures, baseline) ==
    "timeout" \in failures \/ ~(failures \subseteq baseline)

FailureGuards(failures, baseline, inherited) ==
    failures # {} /\ (inherited = "block" \/ NewFailure(failures, baseline))
=============================================================================
