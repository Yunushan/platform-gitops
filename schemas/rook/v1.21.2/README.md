# Rook storage schemas

`storage.json` contains the served v1 OpenAPI schemas for
`cephblockpools.ceph.rook.io` and `cephfilesystems.ceph.rook.io`, extracted without
field changes from [Rook v1.21.2 crds.yaml](https://github.com/rook/rook/blob/v1.21.2/deploy/examples/crds.yaml).
Upstream source SHA-256: `09f50995af37c7ff25ab665beee63e0a7cd7d60e76afd2c610e33807a853bf5c`.
The extracted map uses sorted compact JSON and one trailing newline; SHA-256:
`aa04978acbf546d74b50b91fb66b38de71d182634b3e81fb2b46ce2a2f2cdaaf`.
The upstream Apache-2.0 license is preserved in `LICENSE`.

`cluster.json` contains the served/storage v1 `CephCluster` OpenAPI schema from
the same pinned source, encoded as sorted compact JSON plus a trailing newline.
SHA-256: `9e742a74eb0255274d774525aa3ef8d0dec1ff5275f9bcf046e1e7d6072db46e`.

These schemas validate finite storage resources, not Ceph cluster installation,
operator execution, Kubernetes admission CEL or live storage availability.

`bootstrap.json` preserves all nonempty parsed YAML objects from the same
release's `crds.yaml`, `common.yaml`, `operator.yaml` and `csi-operator.yaml`,
with their source filenames and checksums. It uses sorted compact JSON and a
trailing newline. SHA-256:
`e63630e57672214ba220d484ba42069ad2761cfd1b7ab0220636fc550c28655f`.
Additional source checksums:

- `common.yaml`: `9b518fd1b05bd720a8fcf98a209b9b4ce551ad7b1ed811f0a9c200f74316df7d`
- `operator.yaml`: `fc1f54f17368ac30bb0ebc50e45b9c2fa00a5cfdfea4f4eeef9c8918b38e8eb4`
- `csi-operator.yaml`: `a8d07a406a39dffcea64c2ee37db04adbe20e557ee3805df262b8aef54407359`

All four files originate under
[the pinned deploy/examples directory](https://github.com/rook/rook/tree/v1.21.2/deploy/examples).
The bootstrap renderer selects finite runtime objects from this catalog and
substitutes explicitly reviewed namespace, images, placement and resources.
It performs no network download at render or install time.
