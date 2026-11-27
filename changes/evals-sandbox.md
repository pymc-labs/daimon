- **Enclosed sandboxes for eval cases.** Each eval item can run its tools in a
  sandbox pod on a Docker host or in a Kubernetes namespace. A gateway in the
  pod answers every request from the fake world, and only the provider worker
  that runs the agent reaches its provider. Anthropic, OpenAI and Gemini agents
  run in it through self-hosted environments without their own web tools, and
  the self-hosted harness runs inside it. Package installs are blocked unless
  the `eval_package_registries` feature lets them reach the real registries.
