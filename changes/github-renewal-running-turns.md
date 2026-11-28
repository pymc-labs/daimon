- **The scheduler renews GitHub access only for turns that are running.** An idle
  conversation's GitHub token now lapses, and its next turn gets a fresh one when it
  starts. Before, every conversation that had ever used GitHub was renewed around
  the clock, so the renewal pass slowed as conversations piled up and a brief GitHub
  timeout logged an error.
