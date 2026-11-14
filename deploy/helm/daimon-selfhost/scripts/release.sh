# Sourced by aks.sh and register.sh: the one place that names the Helm release,
# its namespace, the Secrets the scripts create and the values files Helm reads.
# Defaults keep the original pilot names (release acme in namespace daimon).
# Commands that change a release refuse the defaults unless both names were
# exported: on a shared cluster a missed export would target release acme.
RELEASE_NAMES_EXPORTED=
[[ -n ${RELEASE:-} && -n ${NAMESPACE:-} ]] && RELEASE_NAMES_EXPORTED=1
require_release_names() {
  [[ -n $RELEASE_NAMES_EXPORTED ]] || {
    echo "$1 changes a release: export RELEASE and NAMESPACE first" >&2
    exit 1
  }
}
# A namespace holds one release: installing beside another, or deleting the
# namespace under it, would reach a release these scripts do not own.
refuse_other_releases() {
  local listed others
  listed=$(helm list -n "$NAMESPACE" -a -q)
  others=$(grep -vFx -- "$RELEASE" <<< "$listed" | grep -v '^$' || true)
  [[ -z $others ]] || {
    echo "$1 refused: namespace $NAMESPACE also holds Helm release(s) ${others//$'\n'/, }; give $RELEASE a namespace of its own" >&2
    exit 1
  }
}
RELEASE=${RELEASE:-acme}
NAMESPACE=${NAMESPACE:-daimon}
SECRET_PREFIX=${SECRET_PREFIX:-daimon}
for _name in RELEASE NAMESPACE SECRET_PREFIX; do
  [[ ${!_name} =~ ^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$ ]] || {
    echo "$_name must be a lowercase DNS label of at most 40 characters" >&2
    exit 1
  }
done
# Mirrors the chart's d.name helper: "<release>-daimon", cut to 45 characters.
PREFIX="${RELEASE}-daimon"
PREFIX=${PREFIX:0:45}
PREFIX=${PREFIX%-}
# The operator's values file goes first so the generated state (image tag, AOAI
# endpoint and CIDRs, Secret names, registration grants) always matches what the
# scripts created.
HELM_VALUES=()
if [[ -n ${VALUES_FILE:-} ]]; then
  [[ -f $VALUES_FILE ]] || { echo "VALUES_FILE $VALUES_FILE does not exist" >&2; exit 1; }
  HELM_VALUES+=(-f "$VALUES_FILE")
fi
HELM_VALUES+=(-f "$AKS_STATE/aks-values.json")
export RELEASE NAMESPACE SECRET_PREFIX PREFIX
