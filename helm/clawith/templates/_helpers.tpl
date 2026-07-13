{{/*
Expand the name of the chart.
*/}}
{{- define "clawith.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "clawith.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "clawith.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "clawith.labels" -}}
helm.sh/chart: {{ include "clawith.chart" . }}
{{ include "clawith.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "clawith.selectorLabels" -}}
app.kubernetes.io/name: {{ include "clawith.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Build an image reference without emitting a leading slash when the registry
prefix is intentionally empty (for images preloaded into a local cluster).
*/}}
{{- define "clawith.image" -}}
{{- $registry := trimSuffix "/" (default "" .registry) -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{/*
PostgreSQL host
*/}}
{{- define "clawith.postgresql.host" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "%s-postgresql" (include "clawith.fullname" .) }}
{{- else }}
{{- .Values.postgresql.external.host }}
{{- end }}
{{- end }}

{{/*
PostgreSQL port
*/}}
{{- define "clawith.postgresql.port" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.primary.service.port }}
{{- else }}
{{- .Values.postgresql.external.port }}
{{- end }}
{{- end }}

{{/*
PostgreSQL database
*/}}
{{- define "clawith.postgresql.database" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.database }}
{{- else }}
{{- .Values.postgresql.external.database }}
{{- end }}
{{- end }}

{{/*
PostgreSQL username
*/}}
{{- define "clawith.postgresql.username" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.username }}
{{- else }}
{{- .Values.postgresql.external.username }}
{{- end }}
{{- end }}

{{/*
PostgreSQL password
*/}}
{{- define "clawith.postgresql.password" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.password }}
{{- else }}
{{- .Values.postgresql.external.password }}
{{- end }}
{{- end }}

{{/*
PostgreSQL URL with credentials escaped for RFC 3986 userinfo.
*/}}
{{- define "clawith.postgresql.url" -}}
{{- $username := include "clawith.postgresql.username" . -}}
{{- $password := include "clawith.postgresql.password" . -}}
{{- $host := include "clawith.postgresql.host" . -}}
{{- $port := include "clawith.postgresql.port" . -}}
{{- $database := include "clawith.postgresql.database" . -}}
{{- printf "postgresql+asyncpg://%s:%s@%s:%s/%s" ($username | urlquery) ($password | urlquery) $host $port ($database | urlquery) -}}
{{- end }}

{{/*
Redis host
*/}}
{{- define "clawith.redis.host" -}}
{{- if .Values.redis.enabled }}
{{- printf "%s-redis" (include "clawith.fullname" .) }}
{{- else }}
{{- .Values.redis.external.host }}
{{- end }}
{{- end }}

{{/*
Redis port
*/}}
{{- define "clawith.redis.port" -}}
{{- if .Values.redis.enabled }}
{{- .Values.redis.service.port }}
{{- else }}
{{- .Values.redis.external.port }}
{{- end }}
{{- end }}

{{/*
Redis URL. External deployments may opt into password authentication; the
in-cluster Redis remains reachable only through its ClusterIP Service.
*/}}
{{- define "clawith.redis.url" -}}
{{- $host := include "clawith.redis.host" . -}}
{{- $port := include "clawith.redis.port" . -}}
{{- $database := int (default 0 .Values.redis.external.database) -}}
{{- $password := "" -}}
{{- if not .Values.redis.enabled -}}
{{- $password = default "" .Values.redis.external.password -}}
{{- end -}}
{{- if $password -}}
{{- printf "redis://:%s@%s:%s/%d" ($password | urlquery) $host $port $database -}}
{{- else -}}
{{- printf "redis://%s:%s/%d" $host $port $database -}}
{{- end -}}
{{- end }}

{{/*
Secret name
*/}}
{{- define "clawith.secretName" -}}
{{- if .Values.secrets.create }}
{{- printf "%s-secrets" (include "clawith.fullname" .) }}
{{- else }}
{{- required "secrets.existingSecret must be set when secrets.create=false" .Values.secrets.existingSecret }}
{{- end }}
{{- end }}
