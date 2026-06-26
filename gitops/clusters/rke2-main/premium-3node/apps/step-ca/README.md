# step-ca

Optional singleton internal CA for the premium profile. Smallstep's
`step-certificates` chart supports one CA replica, so HA comes from durable
storage and off-cluster backups, not multiple CA pods.

Use `STEP_CA_MODE=bootstrap` only for first deployment or lab bootstrap. For a
long-term company CA, keep key material outside Git and provide chart values
from the private deployment repo or an external secret workflow.
