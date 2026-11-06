---------------------- MODULE IntegrationContract ----------------------
EXTENDS Naturals, FiniteSets, FailurePolicy

CONSTANTS Hosts, Checks, Names, MaxRequests, MaxChanges, MaxSessions

AllHosts == {"claude", "codex", "opencode1", "opencode2", "pi", "neovim"}
ASSUME Hosts \subseteq AllHosts /\ Hosts # {}

(* These are host capabilities, not advisory/strict policy modes. A warning
   cannot be counted as a denied completion, and an editor has no agent gate. *)
Capabilities(h) ==
    [agent |-> h # "neovim",
     waitsForCheck |-> h \in {"codex", "opencode1", "opencode2", "pi"},
     completion |-> CASE h \in {"claude", "codex"} -> "block"
                      [] h = "opencode1" -> "warn"
                      [] h = "opencode2" -> "queue"
                      [] h = "pi" -> "continue"
                      [] OTHER -> "none",
     delivery |-> CASE h = "claude" -> "turn"
                    [] h = "opencode2" -> "context"
                    [] h = "neovim" -> "quickfix"
                    [] OTHER -> "event",
     idleWake |-> h \in {"claude", "opencode2"},
     status |-> h \in {"claude", "pi", "neovim"}]

Kinds == {"inspect", "repair", "publish", "complete"}
Empty == [c \in Checks |-> {}]
Snapshots == [Checks -> SUBSET Names]
NoReply == [ready |-> FALSE, decision |-> "none"]

VARIABLES host, active, busy, reports, baseline, inherited, selected,
          request, reply, requests, changes, session,
          visible, contextPending, contextSent, correctionQueued,
          contexts, warnings, corrections, inTurn, wakes, work

vars == <<host, active, busy, reports, baseline, inherited, selected,
          request, reply, requests, changes, session, visible,
          contextPending, contextSent, correctionQueued, contexts,
          warnings, corrections, inTurn, wakes, work>>

Guarded(scope) ==
    {c \in scope : FailureGuards(reports[c], baseline[c], inherited)}
Guard == Capabilities(host).agent /\ Guarded(Checks) # {}
Notify == Capabilities(host).agent /\
          (\E c \in Checks : FailureGuards(reports[c], baseline[c], "allow"))
SelectedChecks == IF selected = {} THEN Checks ELSE selected

TypeOK ==
    /\ host \in Hosts /\ active \in BOOLEAN /\ busy \in BOOLEAN
    /\ work \in 0..((MaxSessions + 1) * MaxRequests)
    /\ inTurn \in BOOLEAN
    /\ wakes \in 0..(MaxChanges + MaxSessions + 1 + (MaxSessions + 1) * MaxRequests)
    /\ reports \in Snapshots /\ baseline \in Snapshots
    /\ inherited \in {"allow", "block"} /\ selected \subseteq Checks
    /\ request \in Kinds \cup {"none"}
    /\ reply \in [ready : BOOLEAN,
                   decision : {"none", "allow", "deny", "block", "warn", "queue", "continue", "cancelled"}]
    /\ requests \in 0..MaxRequests /\ changes \in 0..MaxChanges /\ session \in 0..MaxSessions
    /\ visible \in [known : BOOLEAN, failures : Snapshots]
    /\ contextPending \in BOOLEAN /\ contextSent \in BOOLEAN /\ correctionQueued \in BOOLEAN
    /\ contexts \in 0..(MaxChanges + MaxSessions + 1)
    /\ warnings \in 0..((MaxSessions + 1) * MaxRequests)
    /\ corrections \in 0..((MaxSessions + 1) * MaxRequests)

Init ==
    /\ host \in Hosts /\ active = TRUE /\ busy \in BOOLEAN
    /\ reports \in Snapshots /\ baseline \in Snapshots
    /\ inherited \in {"allow", "block"} /\ selected \in SUBSET Checks
    /\ request = "none" /\ reply = NoReply
    /\ requests = 0 /\ changes = 0 /\ session = 0
    /\ visible = [known |-> FALSE, failures |-> Empty]
    /\ contextPending = FALSE /\ contextSent = FALSE /\ correctionQueued = FALSE
    /\ contexts = 0 /\ warnings = 0 /\ corrections = 0 /\ wakes = 0 /\ work = 0 /\ inTurn \in BOOLEAN

Request(k) ==
    /\ active /\ request = "none" /\ requests < MaxRequests
    /\ request' = k /\ reply' = NoReply /\ requests' = requests + 1
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   changes, session, visible, contextPending, contextSent,
                   correctionQueued, contexts, warnings, corrections, inTurn, wakes, work>>

CanRespond ==
    active /\ request # "none" /\ ~reply.ready /\
    (request \in {"inspect", "repair"} \/ ~Capabilities(host).agent \/
     ~Capabilities(host).waitsForCheck \/ ~busy)

Decision ==
    CASE request \in {"inspect", "repair"} -> "allow"
      [] request = "publish" ->
           IF Capabilities(host).agent /\ Guarded(SelectedChecks) # {}
           THEN "deny" ELSE "allow"
      [] request = "complete" -> IF Guard THEN Capabilities(host).completion ELSE "allow"

QueuesCorrection == Decision = "queue" \/ (host = "claude" /\ Decision = "block")

Respond ==
    /\ CanRespond
    /\ inTurn' = IF request = "complete" /\
                       (Decision \in {"allow", "warn", "queue"} \/ host = "claude")
                  THEN FALSE ELSE inTurn
    /\ reply' = [ready |-> TRUE, decision |-> Decision]
    /\ warnings' = warnings + IF Decision = "warn" THEN 1 ELSE 0
    /\ corrections' = corrections + IF QueuesCorrection /\ ~correctionQueued THEN 1 ELSE 0
    /\ correctionQueued' = (correctionQueued \/ QueuesCorrection)
    /\ wakes' = wakes + IF QueuesCorrection /\ ~correctionQueued /\
                            (host = "opencode2" \/ ~inTurn)
                         THEN 1 ELSE 0
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   request, requests, changes, session, visible,
                   contextPending, contextSent, contexts, work>>

Acknowledge ==
    /\ request # "none" /\ reply.ready
    /\ request' = "none"
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   reply, requests, changes, session, visible,
                   contextPending, contextSent, correctionQueued,
                   contexts, warnings, corrections, inTurn, wakes, work>>

BeginCheck ==
    /\ active /\ ~busy /\ changes < MaxChanges
    /\ busy' = TRUE
    /\ UNCHANGED <<host, active, reports, baseline, inherited, selected,
                   request, reply, requests, changes, session, visible,
                   contextPending, contextSent, correctionQueued,
                   contexts, warnings, corrections, inTurn, wakes, work>>

(* The result-ordering and baseline rules are specified in Overwatch.tla.
   This layer accepts a completed result at that boundary, then constrains
   what every host must expose and which requests it may hold behind a check. *)
FinishCheck(f) ==
    /\ active /\ busy /\ changes < MaxChanges
    /\ reports' = f /\ busy' = FALSE /\ changes' = changes + 1
    /\ IF f # reports
         THEN /\ contextSent' = FALSE /\ correctionQueued' = FALSE
              /\ contextPending' = FALSE
         ELSE UNCHANGED <<contextSent, correctionQueued, contextPending>>
    /\ UNCHANGED <<host, active, baseline, inherited, selected, request,
                   reply, requests, session, visible, contexts, warnings, corrections, inTurn, wakes, work>>

Report ==
    /\ active /\ ~busy
    /\ visible' = [known |-> TRUE, failures |-> reports]
    /\ contextPending' = (Notify /\ ~contextSent /\ Capabilities(host).delivery \in {"turn", "context"})
    /\ contextSent' = (contextSent \/ (Notify /\ Capabilities(host).delivery = "event"))
    /\ contexts' = contexts + IF Notify /\ ~contextSent /\ Capabilities(host).delivery = "event" THEN 1 ELSE 0
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   request, reply, requests, changes, session,
                   correctionQueued, warnings, corrections, inTurn, wakes, work>>

Deliver ==
    /\ active /\ contextPending
    /\ wakes' = wakes + IF host = "claude" /\ ~inTurn THEN 1 ELSE 0
    /\ contextSent' = TRUE /\ contextPending' = FALSE /\ contexts' = contexts + 1
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   request, reply, requests, changes, session, visible,
                   correctionQueued, warnings, corrections, inTurn, work>>

Stop ==
    /\ active
    /\ inTurn' = FALSE
    /\ active' = FALSE /\ busy' = FALSE /\ request' = "none"
    /\ reply' = [ready |-> TRUE, decision |-> "cancelled"]
    /\ visible' = [known |-> FALSE, failures |-> Empty]
    /\ contextPending' = FALSE
    /\ UNCHANGED <<host, reports, baseline, inherited, selected, requests,
                   changes, session, contextSent, correctionQueued,
                   contexts, warnings, corrections, wakes, work>>

Restart ==
    /\ ~active /\ session < MaxSessions
    /\ inTurn' = FALSE
    /\ active' = TRUE /\ session' = session + 1 /\ busy' = FALSE
    /\ baseline' = reports
    /\ request' = "none" /\ reply' = NoReply /\ requests' = 0
    /\ visible' = [known |-> FALSE, failures |-> Empty]
    /\ contextPending' = FALSE /\ contextSent' = FALSE /\ correctionQueued' = FALSE
    /\ UNCHANGED <<host, reports, inherited, selected, changes, contexts, warnings, corrections, wakes, work>>

StartTurn ==
    /\ active /\ ~inTurn /\ inTurn' = TRUE
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   request, reply, requests, changes, session, visible,
                   contextPending, contextSent, correctionQueued, contexts,
                   warnings, corrections, wakes, work>>

WorkCompleted ==
    /\ active /\ work < (MaxSessions + 1) * MaxRequests
    /\ work' = work + 1 /\ correctionQueued' = FALSE
    /\ UNCHANGED <<host, active, busy, reports, baseline, inherited, selected,
                   request, reply, requests, changes, session, visible,
                   contextPending, contextSent, contexts, warnings, corrections, inTurn, wakes>>

CheckCompletes == \E f \in Snapshots : FinishCheck(f)
Next == (\E k \in Kinds : Request(k)) \/ Respond \/ Acknowledge \/ BeginCheck
        \/ CheckCompletes \/ Report \/ Deliver \/ Stop \/ Restart \/ StartTurn \/ WorkCompleted
        \/ (~active /\ session = MaxSessions /\ UNCHANGED vars)
(* Check completion fairness is the RunsEnd guarantee of the result model.
   Inspection is independently enabled; it never needs that guarantee to reply. *)
Spec == Init /\ [][Next]_vars /\ WF_vars(CheckCompletes)
        /\ WF_vars(Respond) /\ WF_vars(Report) /\ WF_vars(Deliver)

InspectionIndependentOfChecks ==
    active /\ request \in {"inspect", "repair"} /\ ~reply.ready => ENABLED Respond
InspectionAlwaysAllowed ==
    request \in {"inspect", "repair"} /\ reply.ready => reply.decision = "allow"
EditorNeverGuards ==
    host = "neovim" /\ request # "none" /\ reply.ready => reply.decision = "allow"
StoppedSessionsStaySilent == ~active => ~contextPending /\ ~visible.known
HostsWithoutIdleWakeStayIdle == ~Capabilities(host).idleWake => wakes = 0
CorrectionRequestsAreDeduplicated == corrections <= changes + session + work + 1
PendingContextIsCurrent == contextPending => Notify
CompletionUsesHostCapability ==
    request = "complete" /\ reply.ready =>
       reply.decision \in {"allow", Capabilities(host).completion}
EditorOnlyReports ==
    host = "neovim" => contexts = 0 /\ warnings = 0 /\ corrections = 0 /\ wakes = 0
PendingReportsEventuallyResolve == contextPending ~> ~contextPending
FinishedReportsBecomeVisible ==
    (active /\ ~busy) ~> (visible.known \/ ~active)
InspectionEventuallyReplies ==
    (active /\ request \in {"inspect", "repair"} /\ ~reply.ready)
      ~> (reply.ready \/ ~active)
=============================================================================
