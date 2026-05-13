{{/*
Selector / common labels — kept compatible with the rest of the repo so
ArgoCD label selectors work the same as for other apps.
*/}}
{{- define "helm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "helm.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "helm.selectorLabels" -}}
app: {{ .Values.app_name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "helm.labels" -}}
helm.sh/chart: {{ include "helm.chart" . }}
{{ include "helm.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "ingress.hostName" -}}
{{- if eq .Values.env "prod" }}
{{- printf "%s.%s" .Values.app_name .Values.prod_hostname }}
{{- else if eq .Values.env "drc"}}
{{- printf "%s.%s" .Values.app_name .Values.drc_hostname }}
{{- else }}
{{- printf "%s.%s" .Values.app_name .Values.dev_hostname }}
{{- end }}
{{- end }}

{{- define "UseReplicas" -}}
{{- if eq .Values.env "preview" }}{{- printf "true" }}
{{- else if eq .Values.env "dev"}}{{- printf "true" }}
{{- else if eq .Values.env "uat"}}{{- printf "true" }}
{{- else if eq .Values.env "prod"}}{{- printf "false" }}
{{- else if eq .Values.env "drc"}}{{- printf "false" }}
{{- end }}
{{- end }}

{{- define "replicas" -}}
{{- if eq .Values.env "preview" }}{{- .Values.preview.replicas }}
{{- else if eq .Values.env "dev"}}{{- .Values.dev.replicas }}
{{- else if eq .Values.env "uat"}}{{- .Values.uat.replicas }}
{{- else if eq .Values.env "prod"}}{{- .Values.prod.replicas }}
{{- else if eq .Values.env "drc"}}{{- .Values.drc.replicas }}
{{- else }}{{- .Values.replicas }}
{{- end }}
{{- end }}

{{- define "resources" -}}
{{- if eq .Values.env "preview" }}{{- toYaml .Values.preview.resources | nindent 12 }}
{{- else if eq .Values.env "dev"}}{{- toYaml .Values.dev.resources | nindent 12 }}
{{- else if eq .Values.env "uat"}}{{- toYaml .Values.uat.resources | nindent 12 }}
{{- else if eq .Values.env "prod"}}{{- toYaml .Values.prod.resources | nindent 12 }}
{{- else }}{{- toYaml .Values.resources | nindent 12 }}
{{- end }}
{{- end }}
