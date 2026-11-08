#!/bin/sh
# prep -> train -> eval with `after`. Job ids are only known after submit, so chain them in a script.
set -eu
prep=$(gsched submit --file prep.json --print-id)
train=$(gsched submit --file train.json --after "$prep" --print-id)
gsched submit --file eval.json --after "$train"
