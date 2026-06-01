#!/bin/bash
go="/home/bonsai/Desktop/MasterThesis/DC/go/bin/go"
if [ ! -f $go ]; then
    go="/home/bonsai/Desktop/MasterThesis/DC/go/bin/go"
fi
if [ ! -f $go ]; then
    echo -e "\e[1;33mWarning: using system go, results might not be comparable\e[0m"
    go="go"
fi

middlebox_tags=()
case "${1:-middleboxHandler}" in
    middleboxHandler|handler|full)
        ;;
    emptyHandler|empty|emptyhandler)
        middlebox_tags=(-tags emptyhandler)
        ;;
    *)
        echo "Usage: $0 [middleboxHandler|emptyHandler]"
        exit 1
        ;;
esac

(
    cd cmd/client
    "$go" build
)

(
    cd cmd/gateway
    "$go" build
)

(
    cd cmd/middlebox
    "$go" build "${middlebox_tags[@]}"
)
