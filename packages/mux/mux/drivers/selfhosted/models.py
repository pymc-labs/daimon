"""An operator's deployment names; no public model catalogue is consulted."""

from mux.contracts.ids import ModelRef, Provider, Scope
from mux.contracts.resources import ModelAdmission, ModelInfo


class SelfHostedModels:
    def __init__(self, deployments: tuple[str, ...]) -> None:
        if not deployments or any(not name.strip() for name in deployments):
            raise ValueError("at least one deployment name is required")
        self._deployments = deployments

    async def list(self, scope: Scope, provider: Provider) -> tuple[ModelInfo, ...]:
        return (
            tuple(
                ModelInfo(model=ModelRef(provider="selfhosted", id=name))
                for name in self._deployments
            )
            if provider == "selfhosted"
            else ()
        )

    async def validate(self, scope: Scope, model: ModelRef) -> ModelAdmission:
        allowed = (
            model.provider == "selfhosted" and model.id in self._deployments and not model.options
        )
        return ModelAdmission(
            model=model, admitted=allowed, reason=None if allowed else "deployment not configured"
        )
