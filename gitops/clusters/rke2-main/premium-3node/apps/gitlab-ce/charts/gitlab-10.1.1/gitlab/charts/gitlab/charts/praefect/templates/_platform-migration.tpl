{{- define "platform.praefect.migrationPod" -}}
{{- $pod := include "platform.praefect.podSpec" . | fromYaml -}}
{{- $container := last $pod.containers -}}
{{- $_ := unset $container "ports" -}}
{{- $_ := set $container "command" (list "/scripts/entrypoint.sh") -}}
{{- $_ := set $container "args" (list "/usr/local/bin/praefect" "-config" "/etc/gitaly/config.toml" "sql-migrate") -}}
{{- $_ := set $pod "containers" (list $container) -}}
{{- $_ := set $pod "restartPolicy" "Never" -}}
{{- $_ := set $pod "automountServiceAccountToken" false -}}
{{- if $pod.affinity -}}
{{- $_ := unset $pod.affinity "podAntiAffinity" -}}
{{- end -}}
{{- $_ := unset $pod "topologySpreadConstraints" -}}
{{- toYaml $pod -}}
{{- end }}

{{- define "platform.praefect.migrationName" -}}
{{- $config := include (print .Template.BasePath "/configmap.yaml") . -}}
{{- $pod := include "platform.praefect.migrationPod" . -}}
{{- $digest := printf "%s\n%s\n%v" $config $pod .Values.migrationJob.activeDeadlineSeconds | sha256sum | trunc 12 -}}
{{- printf "%s-migrate-%s" (include "fullname" . | trunc 42 | trimSuffix "-") $digest -}}
{{- end }}

{{- define "platform.praefect.schemaAccount" -}}
{{- if or .Values.serviceAccount.enabled .Values.global.serviceAccount.enabled -}}
{{- include "gitlab.serviceAccount.name" . -}}
{{- else -}}
{{- printf "%s-schema" (include "fullname" . | trunc 56 | trimSuffix "-") -}}
{{- end -}}
{{- end }}

{{- define "platform.praefect.schemaGate" -}}
name: wait-for-praefect-migration
image: {{ include "gitlab.kubectl.image" . }}
command:
  - /bin/sh
  - -ec
  - |
    attempts=0
    while [ "$attempts" -lt 120 ]; do
      # Keep kubectl's default client configuration so it loads the projected token.
      conditions=$(timeout 8s kubectl --namespace "$POD_NAMESPACE" get job "$MIGRATION_JOB" \
        -o 'jsonpath={range .status.conditions[*]}{.type}={.status}{"\n"}{end}' 2>/dev/null) || conditions=
      case "$conditions" in
        *Failed=True*) echo "Praefect migration Job failed." >&2; exit 1 ;;
        *Complete=True*) exit 0 ;;
      esac
      attempts=$((attempts + 1))
      sleep 5
    done
    echo "Waiting for the Praefect migration Job to complete." >&2
    exit 1
env:
  - name: POD_NAMESPACE
    value: {{ .Release.Namespace | quote }}
  - name: MIGRATION_JOB
    value: {{ include "platform.praefect.migrationName" . | quote }}
resources:
  requests:
    cpu: 25m
    memory: 32Mi
  limits:
    memory: 128Mi
securityContext:
  runAsNonRoot: true
  runAsUser: 65534
  allowPrivilegeEscalation: false
  capabilities:
    drop: [ALL]
volumeMounts:
  - name: praefect-schema-api
    mountPath: /var/run/secrets/kubernetes.io/serviceaccount
    readOnly: true
{{- end }}
