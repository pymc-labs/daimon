# Product preparation inputs

This offline pack targets N9's committed consumer
`d5fcd425e49ef4bcc4ef4fac247e8469517ffdfe`. It publishes scope-bound
`ProductTurnRecipe` inputs. It does not produce `TurnEvidence` or verdicts.

The existing public TARGET-53 projection omits private bibliographic paths.
Every row in `index.json` pins its actual bytes, separately retaining the original
authored-source pin and the native input pin that links both. The publisher
accepts original catalog bytes or that exact independently verified projection.
The index also
pins the native manifest, preparation publisher and consumer. All 159 cells are
represented. Five Anthropic first turns are AVAILABLE: D11, NEW8, NEW9, NEW10
and NEW37. The other 154 entries are typed PENDING with the missing
preparation/setup boundary. An AVAILABLE input does not resolve later catalog
assertions, follow-ups, resource commands or adapter observations.

The owned caller must be provisioned explicitly: rolling its account creation
back would change the UUID in the exact CREATE request metadata. Preparation
itself runs in a rolled-back transaction, including its commits, and leaves no
session mapping. The provider CREATE acknowledgement uses the native fixture's
existing session ID. The send acknowledgement, root and SSE events are copied
unchanged from the pinned native fixture. Fresh preparation supplies the initial
session snapshot; the tape then appends the native send and stream, not an extra
pre-admitted-session GET. SDK factories refuse ambient settings before recording.
No provider network calls are made.

Run with `PYTHONPATH=tests` to import `judge`. Provision `AdminBindings`, credit,
default agent/environment names and channel configuration through the existing
public setup APIs, then construct `ProductContext` with the explicit owned
destinations. There are no default/fabricated tenant, thread or caller bindings.

```python
from judge.product_preparation_fixtures import (
    availability, provision_preparation_caller, publish_preparation,
)

status = availability(plan, native_recipe, catalog_root=catalog_root)
# PENDING is returned as a typed availability record; publication refuses it.
await provision_preparation_caller(
    context, plan, native_recipe, catalog_root=catalog_root,
)
pin = await publish_preparation(
    context, plan, native_recipe, catalog_root=catalog_root,
)
context.sources = {1: pin}
# Execute through N9's execute_cell(..., product_context=context, ...).
# N9's actual consumer, journal and outcome oracle judge the result.
```

Both APIs revalidate pinned source operations, native pins, selected backend,
owned destination and real channel authorization. Publication requires a
pre-provisioned caller and an empty thread. Missing credit/defaults, shared mode,
busy/deferred preparation and unsupported prerequisites refuse publication.
Every database bind is guarded; routed transactions are PENDING because this
recorder cannot guarantee their complete rollback. Existing connection binds and
owned engines are supported. JSON recipes are specific to the owned scope and
are published to `context.source_root`; transfer their `SourcePin`, not just a
claimed scenario name.

Regenerate the scope-free availability manifest without DB or SDK construction:

```sh
PYTHONPATH=tests uv run --no-sync python -m judge.product_preparation_fixtures \
  --catalog-root packages/testing/fixtures/target53/catalog \
  --repository-root . \
  --output packages/testing/fixtures/product-preparation/index.json
```

This is a first-turn slice. OpenAI/Gemini need a public native preparation seam.
Concurrency, shared threads, remote tool effects and routing need their actual
owned hooks. NEW37 retains its expected failure with table rendering off. These
inputs provide offline product-path coverage, not live provider certification.
