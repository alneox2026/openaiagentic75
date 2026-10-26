# Infrastructure metering: shadow mode

Completed-turn events now carry an `infrastructure_metering` record into the
existing immutable billing ledger. It is collected for analysis only and is not
included in token cost estimates, wallet reservations, or wallet debits.

The record contains the server-derived billing subject and request ID, deployment
environment, project and gateway service/revision context where available, agent
backend and region, gateway wall duration, and assistant content byte count. The
duration is an observed wall-clock proxy; it is not Cloud Run CPU-seconds or an
exact allocation of Cloud Run, Agent Platform, Firestore, or network charges.
Assistant content bytes are not network egress bytes.

This phase does not meter FlutterFlow's direct Firestore operations, Cloud Storage
byte-hours, Storage operations, or external agent-service compute. Do not use the
shadow values as customer charges. Before introducing infrastructure debits,
compare the metered data with detailed Cloud Billing exports, document an explicit
allocation policy for shared and idle costs, and add separate pricing and wallet
settlement tests.
