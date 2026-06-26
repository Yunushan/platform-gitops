# step-ca

Optional internal certificate authority for deployments that need private
PKI instead of only public ACME certificates.

Do not commit CA private keys or CA passwords. For the first private lab or
bootstrap install, render this app with `STEP_CA_MODE=bootstrap`. For a long
term production CA, keep CA material in external secrets or a private secret
workflow and render/supply chart values from the private deployment repo.
