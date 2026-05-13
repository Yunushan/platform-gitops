#!/usr/bin/env bash
set -euo pipefail

: "${PLATFORM_REPO_URL:?Set PLATFORM_REPO_URL to this repository URL.}"
: "${KUBECONFIG:?Set KUBECONFIG to your private kubeconfig path.}"

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

# Install Argo CD with the upstream install manifest for initial bootstrap only.
# After bootstrap, Argo CD should manage itself through gitops/clusters/rke2-main/apps/argocd-ha.
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/ha/install.yaml

# Render root app without storing the private repo URL in git.
sed "s#<THIS_REPO_URL>#${PLATFORM_REPO_URL}#g" gitops/bootstrap/root-app.yaml | kubectl apply -f -
