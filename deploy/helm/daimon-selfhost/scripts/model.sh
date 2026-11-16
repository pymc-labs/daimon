# Sourced by aks.sh and register.sh: the one place that names the model the AOAI
# deployment may run and checks it against Azure. Defaults keep the original pilot
# model; the deployment's name defaults to the model's, and turns run on that deployment.
export AOAI_MODEL=${AOAI_MODEL:-gpt-5.4-mini}
export AOAI_DEPLOYMENT=${AOAI_DEPLOYMENT:-$AOAI_MODEL}
export SELFHOSTED_MODEL=${SELFHOSTED_MODEL:-$AOAI_DEPLOYMENT}
# Read-only: Azure must report AOAI_MODEL as the deployment's model.
verify_aoai_model() {
  AOAI_VERIFIED_MODEL=$(az cognitiveservices account deployment show -g "$RG" -n "$AOAI" \
    --deployment-name "$AOAI_DEPLOYMENT" --query properties.model.name -o tsv) || {
    echo "Existing AOAI account is missing or cannot read deployment $AOAI_DEPLOYMENT; provision it first" >&2
    exit 1
  }
  if [[ "$AOAI_VERIFIED_MODEL" != "$AOAI_MODEL" ]]; then
    echo "AOAI deployment $AOAI_DEPLOYMENT must be backed by AOAI_MODEL=$AOAI_MODEL, not '$AOAI_VERIFIED_MODEL'" >&2
    exit 1
  fi
  export AOAI_VERIFIED_MODEL
}
# Turns run on SELFHOSTED_MODEL: it must be AOAI_DEPLOYMENT, backed by AOAI_MODEL.
verify_runtime_deployment() {
  [[ "$SELFHOSTED_MODEL" == "$AOAI_DEPLOYMENT" ]] || {
    echo "$1 requires SELFHOSTED_MODEL ($SELFHOSTED_MODEL) to be AOAI_DEPLOYMENT ($AOAI_DEPLOYMENT)" >&2
    exit 1
  }
  verify_aoai_model
}
