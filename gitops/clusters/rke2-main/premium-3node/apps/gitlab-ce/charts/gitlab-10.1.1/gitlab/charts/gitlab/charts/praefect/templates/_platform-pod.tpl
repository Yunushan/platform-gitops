{{- define "platform.praefect.podSpec" -}}
{{- $imageCfg := dict "global" .Values.global.image "local" .Values.image -}}
{{- $initImageCfg := include "gitlab.configure.config" .Values | fromYaml -}}
{{- if $.Values.topologySpreadConstraints }}
topologySpreadConstraints: {{- toYaml $.Values.topologySpreadConstraints | nindent 2 }}
{{- end }}
{{- include "gitlab.affinity" . | nindent 0 }}
{{- include "gitlab.nodeSelector" . | nindent 0 }}
{{- include "gitlab.tolerations" . | nindent 0 }}
{{- include "gitlab.priorityClassName" . | nindent 0 }}
initContainers:
  {{- include "gitlab.extraInitContainers" $ | nindent 2 }}
  {{- include "gitlab.certificates.initContainer" $ | nindent 2 }}
  - name: configure
    command: ['sh', '/config/configure']
    image: {{ include "gitlab.configure.image" (dict "root" $ "image" .Values.init.image) | quote }}
    {{- include "gitlab.image.pullPolicy" $initImageCfg | indent 4 }}
    {{- include "gitlab.init.containerSecurityContext" $ | indent 4 }}
    env:
    {{- include "gitlab.extraEnv" $ | nindent 4 }}
    {{- include "gitlab.extraEnvFrom" (dict "root" $ "local" .) | nindent 4 }}
    {{- include "gitlab.timeZone.env" . | nindent 4 }}
    volumeMounts:
    {{- include "gitlab.extraVolumeMounts" $ | nindent 4 }}
    - name: praefect-config
      mountPath: /config
      readOnly: true
    - name: init-praefect-secrets
      mountPath: /init-config
      readOnly: true
    - name: praefect-secrets
      mountPath: /init-secrets
      readOnly: false
    resources:
      {{- toYaml $.Values.init.resources | nindent 6 }}
{{- include "gitlab.podSecurityContext" .Values.securityContext | nindent 0 }}
{{- include "gitlab.image.pullSecrets" $imageCfg | indent 0 }}
{{- if or .Values.serviceAccount.enabled .Values.global.serviceAccount.enabled }}
serviceAccountName: {{ include "gitlab.serviceAccount.name" . }}
{{- end }}
containers:
  {{- include "gitlab.extraContainers" . | nindent 0 }}
- name: {{ $.Chart.Name }}
  image: "{{ $.Values.image.repository }}:{{ coalesce $.Values.image.tag (include "gitlab.parseAppVersion" (dict "appVersion" $.Chart.AppVersion "prepend" "true")) }}{{ include "gitlab.image.tagSuffix" . }}"
  {{- include "gitlab.image.pullPolicy" $imageCfg | indent 2 }}
  {{- include "gitlab.containerSecurityContext" $ | indent 2 }}
  ports:
    - containerPort: {{ include "gitlab.praefect.internalPort" $ }}
  {{- if $.Values.global.praefect.tls.enabled }}
    - containerPort: {{ include "gitlab.praefect.tls.internalPort" $ }}
  {{- end }}
  {{- if .Values.metrics.enabled }}
    - containerPort: {{ .Values.metrics.port }}
      name: http-metrics
  {{- end }}
  env:
  - name: CONFIG_TEMPLATE_DIRECTORY
    value: '/etc/gitaly/templates'
  - name: CONFIG_DIRECTORY
    value: '/etc/gitaly'
  - name: PRAEFECT_CONFIG_FILE
    value: '/etc/gitaly/config.toml'
  - name: SSL_CERT_DIR
    value: '/etc/ssl/certs'
  - name: PRAEFECT_PROMETHEUS_LISTEN_ADDR
    value: ':{{ .Values.metrics.port }}'
  - name: USE_PRAEFECT_SERVICE
    value: '1'
  - name: PRAEFECT_AUTO_MIGRATE
    value: '{{ .Values.global.praefect.autoMigrate | int }}'
  - name: NTP_HOST
    value: '{{ .Values.global.praefect.ntpHost }}'
  {{- include "gitlab.godebug.env" $ | nindent 2 }}
  {{- include "gitlab.tracing.env" . | nindent 2 }}
  {{- include "gitlab.extraEnv" . | nindent 2 }}
  {{- include "gitlab.extraEnvFrom" (dict "root" $ "local" .) | nindent 2 }}
  {{- include "gitlab.timeZone.env" . | nindent 2 }}
  resources:
    {{- toYaml .Values.resources | nindent 4 }}
  terminationMessagePath: /dev/termination-log
  terminationMessagePolicy: File
  volumeMounts:
    {{- include "gitlab.extraVolumeMounts" . | nindent 4 }}
    {{- include "gitlab.certificates.volumeMount" . | nindent 4 }}
    - name: praefect-secrets
      mountPath: /etc/gitlab-secrets
      readOnly: true
    - name: praefect-config
      mountPath: /etc/gitaly/templates
dnsPolicy: ClusterFirst
restartPolicy: Always
terminationGracePeriodSeconds: 30
volumes:
- configMap:
    defaultMode: 420
    name: {{ template "fullname" . }}
  name: praefect-config
- emptyDir:
    medium: Memory
  name: praefect-secrets
- name: init-praefect-secrets
  projected:
    defaultMode: 288
    sources:
    - secret:
        items:
        - key: {{ template "gitlab.gitaly.authToken.key" . }}
          path: gitaly_token
        name: {{ template "gitlab.gitaly.authToken.secret" . }}
    - secret:
        items:
        - key: {{ template "gitlab.praefect.authToken.key" . }}
          path: praefect_token
        name: {{ template "gitlab.praefect.authToken.secret" . }}
    - secret:
        items:
        - key: {{ template "gitlab.praefect.dbSecret.key" . }}
          path: db_password
        name: {{ template "gitlab.praefect.dbSecret.secret"  . }}
    {{- if $.Values.global.praefect.tls.enabled }}
    - secret:
        name: {{ template "gitlab.praefect.tls.secret" $ }}
        items:
          - key: "tls.crt"
            path: "praefect.crt"
          - key: "tls.key"
            path: "praefect.key"
    {{- end }}
{{- include "gitlab.certificates.volumes" . | nindent 0 }}
{{- end }}
