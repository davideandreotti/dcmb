//go:build !emptyhandler

package main

import (
	"encoding/json"
	"log"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

type MessageType struct {
	Method  string
	Uri     string
	Schemas []Schema
}

type Schema struct {
	JsonGetter         func(any) any
	JsonSchemaFilename string
}

// Accept POST and GET for the init endpoint to allow testing with GET as well
var initMessageType = MessageType{"POST|GET", "/function/init", []Schema{}}
var productsMessageType = MessageType{"POST|GET", "/function/product-catalog-api/products", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/products-request-schema.json"}}}
var categoriesMessageType = MessageType{"POST|GET", "/function/product-catalog-api/categories", []Schema{}}
var buildProductMessageType = MessageType{"POST|GET", "/function/product-catalog-builder/product", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/retail-stream-schema-ingress.json"}, {func(jsonBody any) any { return jsonBody.(map[string]any)["data"] }, "schemas/product-create-schema.json"}}}
var productImageMessageType = MessageType{"POST|GET", "/function/product-catalog-builder/image", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/retail-stream-schema-ingress.json"}, {func(jsonBody any) any { return jsonBody.(map[string]any)["data"] }, "schemas/product-image-schema.json"}}}
var productPurchaseMessageType = MessageType{"POST|GET", "/function/product-purchase", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/product-purchase-schema.json"}}}
var photographerRegisterMessageType = MessageType{"POST|GET", "/function/product-photos-register", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/retail-stream-schema-ingress.json"}, {func(jsonBody any) any { return jsonBody.(map[string]any)["data"] }, "schemas/user-update-phone-schema.json"}}}
var photoRequestMessageType = MessageType{"POST|GET", "/function/product-photos/request", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/retail-stream-schema-ingress.json"}, {func(jsonBody any) any { return jsonBody.(map[string]any)["data"] }, "schemas/photo-request-schema.json"}}}
var photoAssignmentMessageType = MessageType{"POST|GET", "/function/product-photos/photos", []Schema{{func(jsonBody any) any { return jsonBody }, "schemas/photo-assignment-schema.json"}}}

var messageTypes = []MessageType{
	initMessageType,
	productsMessageType,
	categoriesMessageType,
	buildProductMessageType,
	productImageMessageType,
	productPurchaseMessageType,
	photographerRegisterMessageType,
	photoRequestMessageType,
	photoAssignmentMessageType,
}

func (m *MessageType) MatchRequest(method string, uri string) bool {
	//	return method == m.Method && uri == m.Uri
	if uri != m.Uri {
		return false
	}
	// support multiple methods separated by '|' or '*' wildcard
	if m.Method == "*" {
		return true
	}
	for _, mm := range strings.Split(m.Method, "|") {
		if method == mm {
			return true
		}
	}
	return false
}

func (m *MessageType) ValidateSchemas(body []byte) bool {
	var jsonBody any
	if len(m.Schemas) == 0 && len(body) == 0 {
		return true
	}
	if err := json.Unmarshal(body, &jsonBody); err != nil {
		return false
	}
	for _, schema := range m.Schemas {
		jsonFragment := schema.JsonGetter(jsonBody)
		if err := ValidateJSONSchema(jsonFragment, schema.JsonSchemaFilename); err != nil {
			log.Println(err)
			return false
		}
	}

	return true
}

func ValidateJSONSchema(jsonFragment any, jsonSchemaFilename string) error {
	sch, err := jsonschema.Compile(jsonSchemaFilename)
	if err != nil {
		return err
	}
	if err = sch.Validate(jsonFragment); err != nil {
		return err
	}
	return nil
}
