{{- define "hooklane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hooklane.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "hooklane.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "hooklane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "hooklane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hooklane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "hooklane.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "hooklane.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "hooklane.image" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{- define "hooklane.validateRedisURL" -}}
{{- $redisURL := .Values.config.redisURL | toString -}}
{{- if not (or (hasPrefix "redis://" $redisURL) (hasPrefix "rediss://" $redisURL)) -}}
{{- fail "config.redisURL must use redis:// or rediss://; use redisURLSecret for credentials" -}}
{{- end -}}
{{- if or (contains "@" $redisURL) (contains "?" $redisURL) (contains "#" $redisURL) (regexMatch "[[:space:]]" $redisURL) -}}
{{- fail "config.redisURL must be credential-free and must not contain query, fragment, or whitespace; use redisURLSecret for credentials" -}}
{{- end -}}
{{- end }}
