# Secrets

Credentials live only in this directory, which is Git-ignored except for this file. Agents, shell commands and review tools never open, list, search, copy or print anything here (AGENTS.md section 6). Only project Python loads a credential at runtime and passes it straight to the SDK without logging. No credential is needed at present: the models run offline.
