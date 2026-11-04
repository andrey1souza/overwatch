---------------------------- MODULE Overwatch ----------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Names, MaxEdits, MaxAhead, RedBudget, Timeout

ASSUME "timeout" \notin Names

Modes == {"advisory", "default", "strict"}
Versions == 0..MaxEdits
Results == SUBSET Names
TimedOut == "timeout"
Outcomes == SUBSET (Names \cup {TimedOut})
NoMessage == [held |-> FALSE, waking |-> FALSE, failures |-> {}]
NotTold == [kind |-> "none", fresh |-> {}]

VARIABLES
    mode, truth, head,
    busy, from, quiet, installed,
    report, baseline, wasFailing,
    outbox, told, flush,
    turn, toldThisTurn, promptQueued,
    knows, done,
    elapsed, redEdits, escalated

vars == <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
          outbox, told, flush, turn, toldThisTurn, promptQueued, knows, done,
          elapsed, redEdits, escalated>>

limits == <<elapsed, redEdits, escalated>>

TypeOK ==
    /\ mode \in Modes
    /\ truth \in [Versions -> Results]
    /\ head \in Versions
    /\ busy \in BOOLEAN /\ from \in Versions /\ quiet \in BOOLEAN /\ installed \in BOOLEAN
    /\ report \in [known : BOOLEAN, at : Versions, failures : Outcomes]
    /\ baseline \in [known : BOOLEAN, failures : Outcomes]
    /\ wasFailing \in BOOLEAN
    /\ outbox \in [held : BOOLEAN, waking : BOOLEAN, failures : Outcomes]
    /\ told \in [kind : {"none", "failed", "recovered"}, fresh : Outcomes]
    /\ flush \in BOOLEAN
    /\ turn \in {"idle", "running"}
    /\ toldThisTurn \in BOOLEAN /\ promptQueued \in BOOLEAN
    /\ knows \in Outcomes
    /\ done \in [known : BOOLEAN, at : Versions, mode : Modes]
    /\ elapsed \in 0..Timeout /\ redEdits \in 0..RedBudget /\ escalated \in BOOLEAN

Failing == report.known /\ report.failures # {}
Regressed(f) == TimedOut \in f \/ (baseline.known /\ ~(f \subseteq baseline.failures))
Fresh == report.known /\ report.at = head /\ ~quiet /\ ~busy

Guard(m) ==
    CASE m = "advisory" -> FALSE
      [] m = "default"  -> Failing /\ Regressed(report.failures)
      [] m = "strict"   -> Failing \/ ~Fresh

Ahead == IF report.known THEN head - report.at ELSE head

CanEdit(m) ==
    CASE m = "advisory" -> TRUE
      [] m = "default"  -> Ahead < MaxAhead
      [] m = "strict"   -> Ahead < MaxAhead /\ redEdits < RedBudget

Init ==
    /\ mode \in Modes
    /\ truth \in [Versions -> Results]
    /\ head = 0
    /\ busy = TRUE /\ from = 0 /\ quiet = FALSE /\ installed = FALSE
    /\ report = [known |-> FALSE, at |-> 0, failures |-> {}]
    /\ baseline = [known |-> FALSE, failures |-> {}]
    /\ wasFailing = FALSE
    /\ outbox = NoMessage /\ told = NotTold /\ flush = FALSE
    /\ turn = "idle" /\ toldThisTurn = FALSE /\ promptQueued = FALSE
    /\ knows = {}
    /\ done = [known |-> FALSE, at |-> 0, mode |-> "default"]
    /\ elapsed = 0 /\ redEdits = 0 /\ escalated = FALSE

Queue(signature, message) ==
    IF told = signature /\ ~outbox.held
    THEN UNCHANGED <<outbox, told, flush>>
    ELSE /\ told' = signature
         /\ outbox' = message
         /\ flush' = TRUE

Deliver(v, f) ==
    LET b == IF baseline.known THEN baseline.failures ELSE IF head = 0 THEN f ELSE {}
        isRegression == ~(f \subseteq b)
    IN /\ report' = [known |-> TRUE, at |-> v, failures |-> f]
       /\ baseline' = [known |-> TRUE, failures |-> b]
       /\ wasFailing' = isRegression
       /\ redEdits' = IF isRegression THEN redEdits ELSE 0
       /\ IF isRegression
            THEN Queue([kind |-> "failed", fresh |-> f \ b], [held |-> TRUE, waking |-> TRUE, failures |-> f])
          ELSE IF ~wasFailing
            THEN UNCHANGED <<outbox, told, flush>>
          ELSE IF outbox.held
            THEN /\ outbox' = NoMessage
                 /\ told' = NotTold
                 /\ UNCHANGED flush
          ELSE Queue([kind |-> "recovered", fresh |-> {}], [held |-> TRUE, waking |-> FALSE, failures |-> f])

Edit ==
    /\ turn = "running"
    /\ head < MaxEdits
    /\ CanEdit(mode)
    /\ head' = head + 1
    /\ quiet' = TRUE
    /\ redEdits' = IF wasFailing /\ mode = "strict" THEN redEdits + 1 ELSE redEdits
    /\ UNCHANGED <<mode, truth, busy, from, installed, report, baseline, wasFailing,
                   outbox, told, flush, turn, toldThisTurn, promptQueued, knows, done,
                   elapsed, escalated>>

QuietElapses ==
    /\ quiet
    /\ installed
    /\ quiet' = FALSE
    /\ busy' = TRUE
    /\ from' = head
    /\ elapsed' = 0
    /\ UNCHANGED <<mode, truth, head, installed, report, baseline, wasFailing,
                   outbox, told, flush, turn, toldThisTurn, promptQueued, knows, done,
                   redEdits, escalated>>

Tick ==
    /\ busy
    /\ elapsed < Timeout
    /\ elapsed' = elapsed + 1
    /\ UNCHANGED <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
                   outbox, told, flush, turn, toldThisTurn, promptQueued, knows, done,
                   redEdits, escalated>>

RunEnds ==
    /\ busy
    /\ \E v \in from..head : Deliver(v, truth[v])
    /\ busy' = FALSE
    /\ installed' = TRUE
    /\ UNCHANGED <<mode, truth, head, from, quiet, turn, toldThisTurn, promptQueued, knows, done,
                   elapsed, escalated>>

TimesOut ==
    /\ busy
    /\ elapsed = Timeout
    /\ report' = [known |-> TRUE, at |-> from, failures |-> {TimedOut}]
    /\ wasFailing' = TRUE
    /\ Queue([kind |-> "failed", fresh |-> {TimedOut}], [held |-> TRUE, waking |-> TRUE, failures |-> {TimedOut}])
    /\ busy' = FALSE
    /\ installed' = TRUE
    /\ UNCHANGED <<mode, truth, head, from, quiet, baseline, turn, toldThisTurn, promptQueued, knows, done,
                   elapsed, redEdits, escalated>>

Held ==
    IF turn = "running" THEN toldThisTurn ELSE promptQueued \/ escalated \/ ~outbox.waking

Flush ==
    /\ flush
    /\ flush' = FALSE
    /\ IF outbox.held /\ ~Held
         THEN /\ outbox' = NoMessage
              /\ knows' = outbox.failures
              /\ IF turn = "running"
                   THEN toldThisTurn' = TRUE /\ UNCHANGED promptQueued
                   ELSE promptQueued' = TRUE /\ UNCHANGED toldThisTurn
         ELSE UNCHANGED <<outbox, knows, toldThisTurn, promptQueued>>
    /\ UNCHANGED <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
                   told, turn, done>>
    /\ UNCHANGED limits

TurnStarts ==
    /\ turn = "idle"
    /\ turn' = "running"
    /\ toldThisTurn' = promptQueued
    /\ promptQueued' = FALSE
    /\ flush' = TRUE
    /\ escalated' = FALSE
    /\ redEdits' = IF escalated THEN 0 ELSE redEdits
    /\ UNCHANGED <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
                   outbox, told, knows, done, elapsed>>

WokenTurnStarts == promptQueued /\ ~escalated /\ TurnStarts

TurnCompletes ==
    /\ turn = "running"
    /\ IF Guard(mode)
         THEN /\ toldThisTurn' = TRUE
              /\ knows' = IF Failing THEN report.failures ELSE knows
              /\ UNCHANGED <<turn, flush, done>>
         ELSE /\ turn' = "idle"
              /\ flush' = TRUE
              /\ done' = [known |-> TRUE, at |-> head, mode |-> mode]
              /\ UNCHANGED <<toldThisTurn, knows>>
    /\ UNCHANGED <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
                   outbox, told, promptQueued>>
    /\ UNCHANGED limits

Escalates ==
    /\ mode = "strict"
    /\ turn = "running"
    /\ wasFailing
    /\ redEdits = RedBudget
    /\ turn' = "idle"
    /\ escalated' = TRUE
    /\ knows' = report.failures
    /\ UNCHANGED <<mode, truth, head, busy, from, quiet, installed, report, baseline, wasFailing,
                   outbox, told, flush, toldThisTurn, promptQueued, done, elapsed, redEdits>>

Next == Edit \/ QuietElapses \/ Tick \/ RunEnds \/ TimesOut \/ Flush \/ TurnStarts \/ TurnCompletes \/ Escalates

Spec ==
    /\ Init /\ [][Next]_vars
    /\ WF_vars(QuietElapses) /\ WF_vars(Tick) /\ WF_vars(TimesOut) /\ WF_vars(Flush)
    /\ WF_vars(WokenTurnStarts) /\ WF_vars(TurnCompletes) /\ WF_vars(Escalates)

ModesNest ==
    /\ Guard("advisory") => Guard("default")
    /\ Guard("default") => Guard("strict")
    /\ CanEdit("strict") => CanEdit("default")
    /\ CanEdit("default") => CanEdit("advisory")

EditsStayCloseToResults ==
    mode # "advisory" => Ahead <= MaxAhead

RedEditsAreBounded ==
    mode = "strict" => redEdits <= RedBudget

RunsEnd ==
    busy ~> ~busy

EditingResumes ==
    ~CanEdit(mode) ~> (CanEdit(mode) \/ escalated)

StrictCompletesOnlyOnVerifiedGreen ==
    done.known /\ done.mode = "strict" => truth[done.at] = {}

BaselineNeverAbsorbsEdits ==
    baseline.known => baseline.failures \subseteq truth[0]

EveryEditIsChecked ==
    <>[](report.known /\ report.at = head)

RegressionsReachAgent ==
    <>[]((truth[head] \ truth[0]) \subseteq knows \/ TimedOut \in knows \/ escalated)
=============================================================================
