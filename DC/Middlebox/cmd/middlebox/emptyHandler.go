//go:build emptyhandler

package main

import (
	"net/http"
)

func initializeValidation() error {
	return nil
}

func processRequest(inputData *http.Request) (bool, string, any) {
	return true, "", nil
}

func processResponse(inputData *http.Response, user string, messageType any) {
}
