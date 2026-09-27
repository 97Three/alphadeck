#!/usr/bin/env node
// Compatibility launcher; the implementation lives in the Python service.
require('../shared/python-entrypoint').runPython("alphadeck_platform.diagnostics_cli", ["models"]);
