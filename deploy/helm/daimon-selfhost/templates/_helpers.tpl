{{- define "d.name" -}}{{ printf "%s-daimon" .Release.Name | trunc 45 | trimSuffix "-" }}{{- end -}}
{{- define "d.image" -}}
{{- if .root.Values.images.registry }}{{ .root.Values.images.registry }}/{{ end -}}
{{ .image.repository }}{{ if .image.digest }}@{{ .image.digest }}{{ else }}:{{ .image.tag }}{{ end }}
{{- end -}}
{{- define "d.csiMount" -}}
{{- if .Values.keyVaultCSI.enabled }}
volumeMounts: [{name: secrets-store, mountPath: /mnt/secrets-store, readOnly: true}]
{{- end }}
{{- end -}}
{{- define "d.csiVolume" -}}
{{- if .Values.keyVaultCSI.enabled }}
volumes:
  - name: secrets-store
    csi:
      driver: secrets-store.csi.k8s.io
      readOnly: true
      volumeAttributes:
        secretProviderClass: {{ required "keyVaultCSI.secretProviderClass required" .Values.keyVaultCSI.secretProviderClass }}
{{- end }}
{{- end -}}
{{- define "d.mcpTLSSecret" -}}
{{- default (printf "%s-mcp-tls" (include "d.name" .)) .Values.mcpTLS.existingSecret -}}
{{- end -}}
{{- define "d.mcpURL" -}}
{{- printf "https://%s-mcp:%v/mcp" (include "d.name" .) .Values.services.mcp.port -}}
{{- end -}}
