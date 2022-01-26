#!/bin/bash -e
set -euo pipefail

echo 'running the "pre" iteration, generating dcdh.npz with a longer cut-off in step_000'
pushd pre
csg_inverse --options settings.xml
popd

echo 'copying step_000 from pre'
cp -r pre/step_000 ./

echo 'running the main iterations with shorter cut-off (faster)'
csg_inverse --options settings.xml
