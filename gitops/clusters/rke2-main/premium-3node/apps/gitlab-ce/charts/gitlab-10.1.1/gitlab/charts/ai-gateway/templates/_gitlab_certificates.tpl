{{/*
Noop templates for certificate handling.

When this chart is deployed standalone, these templates produce no output.
When included as a sub-chart of the GitLab chart, the parent chart's
templates/_certificates.tpl definitions take precedence, injecting the
real CA certificate trust init container, volume mounts, and volumes.

See: https://gitlab.com/gitlab-org/charts/gitlab/-/blob/master/templates/_certificates.tpl
*/}}

{{- define "gitlab.certificates.initContainer" -}}
{{- end -}}

{{- define "gitlab.certificates.volumeMount" -}}
{{- end -}}

{{- define "gitlab.certificates.volumes" -}}
{{- end -}}
