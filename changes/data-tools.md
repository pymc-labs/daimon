- **Agents that read data sources get tools to search and read them.**
  `data_sources`, `data_search`, `data_get`, `data_file` and `data_sql` answer
  only with what everyone who will see the answer may read, and bill the
  person who asked. After such a read, a write that would not ask first shows
  a card in chat and is refused in a routine
  ([Permissions](docs/permissions.md#data-reads)).
