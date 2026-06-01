#!/bin/sh
set -eu

/restart_aesm.sh
cd /opt/middleboxsgxshield
exec gramine-sgx middleboxsgx

