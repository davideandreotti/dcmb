package trace

const (
	ClientRequestScheduled uint32 = 1001
	ClientRequestStart     uint32 = 1002
	ClientTCPConnectStart  uint32 = 1003
	ClientTCPConnectDone   uint32 = 1004
	ClientTLSStart         uint32 = 1005
	ClientTLSDone          uint32 = 1006
	ClientRequestSent      uint32 = 1007
	ClientResponseDone     uint32 = 1008
	ClientRequestError     uint32 = 1009
	ClientResponseFirst    uint32 = 1010
	ClientTLSResumed       uint32 = 1011
	ClientConnectionBind   uint32 = 1012

	GatewayClientAccepted        uint32 = 2001
	GatewayQueueEnter            uint32 = 2002
	GatewayQueueLeave            uint32 = 2003
	GatewayQueueFull             uint32 = 2004
	GatewayQueueTimeout          uint32 = 2005
	GatewayWorkerSelectStart     uint32 = 2006
	GatewayWorkerSelectDone      uint32 = 2007
	GatewayBackendDialStart      uint32 = 2008
	GatewayBackendDialDone       uint32 = 2009
	GatewaySpliceStart           uint32 = 2010
	GatewaySpliceDone            uint32 = 2011
	GatewayRequestDropped        uint32 = 2012
	GatewayContainerCreate       uint32 = 2013
	GatewayContainerStartDone    uint32 = 2014
	GatewayContainerIPAssigned   uint32 = 2015
	GatewayContainerReady        uint32 = 2016
	GatewayContainerRemove       uint32 = 2017
	GatewayContainerRemoved      uint32 = 2018
	GatewayClientConnectionBind  uint32 = 2019
	GatewayBackendConnectionBind uint32 = 2020
	GatewayClientToBackendFirst  uint32 = 2021
	GatewayBackendToClientFirst  uint32 = 2022

	MiddleboxTLSGetCertStart          uint32 = 3001
	MiddleboxTLSGetCertDone           uint32 = 3002
	MiddleboxDelegationCacheHit       uint32 = 3003
	MiddleboxDelegationMiss           uint32 = 3004
	MiddleboxDelegationFetch          uint32 = 3005
	MiddleboxDelegationFetched        uint32 = 3006
	MiddleboxAttestationStart         uint32 = 3007
	MiddleboxAttestationDone          uint32 = 3008
	MiddleboxValidationStart          uint32 = 3009
	MiddleboxValidationDone           uint32 = 3010
	MiddleboxRequestStart             uint32 = 3011
	MiddleboxRequestDone              uint32 = 3012
	MiddleboxUpstreamDialStart        uint32 = 3013
	MiddleboxUpstreamDialDone         uint32 = 3014
	MiddleboxUpstreamTLSStart         uint32 = 3015
	MiddleboxUpstreamTLSDone          uint32 = 3016
	MiddleboxProxyError               uint32 = 3017
	MiddleboxResponseValidationStart  uint32 = 3018
	MiddleboxResponseValidationDone   uint32 = 3019
	MiddleboxSchemaCompileStart       uint32 = 3020
	MiddleboxSchemaCompileDone        uint32 = 3021
	MiddleboxConnectionAccepted       uint32 = 3022
	MiddleboxTraceConnectionBind      uint32 = 3023
	MiddleboxDelegationConnectionBind uint32 = 3024
	MiddleboxDelegationFetchByID      uint32 = 3025
	MiddleboxDelegationFetchedByID    uint32 = 3026
	MiddleboxAttestationStartByID     uint32 = 3027
	MiddleboxAttestationDoneByID      uint32 = 3028
	MiddleboxUpstreamRequestSent      uint32 = 3029
	MiddleboxUpstreamResponseFirst    uint32 = 3030
	MiddleboxDownstreamResponseFirst  uint32 = 3031
	MiddleboxDownstreamResponseDone   uint32 = 3032
	MiddleboxUpstreamConnectionBind   uint32 = 3033

	CertServerRequest          uint32 = 4001
	CertServerGenerate         uint32 = 4002
	CertServerGenerateDone     uint32 = 4003
	CertServerResponse         uint32 = 4004
	CertServerError            uint32 = 4005
	CertServerQuoteVerify      uint32 = 4006
	CertServerQuoteDone        uint32 = 4007
	CertServerRequestByID      uint32 = 4008
	CertServerGenerateByID     uint32 = 4009
	CertServerGenerateDoneByID uint32 = 4010
	CertServerResponseByID     uint32 = 4011
	CertServerErrorByID        uint32 = 4012
	CertServerQuoteVerifyByID  uint32 = 4013
	CertServerQuoteDoneByID    uint32 = 4014

	// Application/request server lifecycle.
	RequestServerRequestStart  uint32 = 5001
	RequestServerResponseStart uint32 = 5002
	RequestServerResponseDone  uint32 = 5003
)

var eventNames = map[uint32]string{
	ClientRequestScheduled: "client_request_scheduled",
	ClientRequestStart:     "client_request_start",
	ClientTCPConnectStart:  "client_tcp_connect_start",
	ClientTCPConnectDone:   "client_tcp_connect_done",
	ClientTLSStart:         "client_tls_start",
	ClientTLSDone:          "client_tls_done",
	ClientRequestSent:      "client_request_sent",
	ClientResponseDone:     "client_response_done",
	ClientRequestError:     "client_request_error",
	ClientResponseFirst:    "client_response_first_byte",
	ClientTLSResumed:       "client_tls_resumed",
	ClientConnectionBind:   "client_connection_bind",

	GatewayClientAccepted:        "gateway_client_accepted",
	GatewayQueueEnter:            "gateway_queue_enter",
	GatewayQueueLeave:            "gateway_queue_leave",
	GatewayQueueFull:             "gateway_queue_full",
	GatewayQueueTimeout:          "gateway_queue_timeout",
	GatewayWorkerSelectStart:     "gateway_worker_select_start",
	GatewayWorkerSelectDone:      "gateway_worker_select_done",
	GatewayBackendDialStart:      "gateway_backend_dial_start",
	GatewayBackendDialDone:       "gateway_backend_dial_done",
	GatewaySpliceStart:           "gateway_splice_start",
	GatewaySpliceDone:            "gateway_splice_done",
	GatewayRequestDropped:        "gateway_request_dropped",
	GatewayContainerCreate:       "gateway_container_create",
	GatewayContainerStartDone:    "gateway_container_start_done",
	GatewayContainerIPAssigned:   "gateway_container_ip_assigned",
	GatewayContainerReady:        "gateway_container_ready",
	GatewayContainerRemove:       "gateway_container_remove",
	GatewayContainerRemoved:      "gateway_container_removed",
	GatewayClientConnectionBind:  "gateway_client_connection_bind",
	GatewayBackendConnectionBind: "gateway_backend_connection_bind",
	GatewayClientToBackendFirst:  "gateway_client_to_backend_first_byte",
	GatewayBackendToClientFirst:  "gateway_backend_to_client_first_byte",

	MiddleboxTLSGetCertStart:          "middlebox_tls_get_cert_start",
	MiddleboxTLSGetCertDone:           "middlebox_tls_get_cert_done",
	MiddleboxDelegationCacheHit:       "middlebox_delegation_cache_hit",
	MiddleboxDelegationMiss:           "middlebox_delegation_miss",
	MiddleboxDelegationFetch:          "middlebox_delegation_fetch",
	MiddleboxDelegationFetched:        "middlebox_delegation_fetched",
	MiddleboxAttestationStart:         "middlebox_attestation_start",
	MiddleboxAttestationDone:          "middlebox_attestation_done",
	MiddleboxValidationStart:          "middlebox_validation_start",
	MiddleboxValidationDone:           "middlebox_validation_done",
	MiddleboxRequestStart:             "middlebox_request_start",
	MiddleboxRequestDone:              "middlebox_request_done",
	MiddleboxUpstreamDialStart:        "middlebox_upstream_dial_start",
	MiddleboxUpstreamDialDone:         "middlebox_upstream_dial_done",
	MiddleboxUpstreamTLSStart:         "middlebox_upstream_tls_start",
	MiddleboxUpstreamTLSDone:          "middlebox_upstream_tls_done",
	MiddleboxProxyError:               "middlebox_proxy_error",
	MiddleboxResponseValidationStart:  "middlebox_response_validation_start",
	MiddleboxResponseValidationDone:   "middlebox_response_validation_done",
	MiddleboxSchemaCompileStart:       "middlebox_schema_compile_start",
	MiddleboxSchemaCompileDone:        "middlebox_schema_compile_done",
	MiddleboxConnectionAccepted:       "middlebox_connection_accepted",
	MiddleboxTraceConnectionBind:      "middlebox_trace_connection_bind",
	MiddleboxDelegationConnectionBind: "middlebox_delegation_connection_bind",
	MiddleboxDelegationFetchByID:      "middlebox_delegation_fetch_by_id",
	MiddleboxDelegationFetchedByID:    "middlebox_delegation_fetched_by_id",
	MiddleboxAttestationStartByID:     "middlebox_attestation_start_by_id",
	MiddleboxAttestationDoneByID:      "middlebox_attestation_done_by_id",
	MiddleboxUpstreamRequestSent:      "middlebox_upstream_request_sent",
	MiddleboxUpstreamResponseFirst:    "middlebox_upstream_response_first_byte",
	MiddleboxDownstreamResponseFirst:  "middlebox_downstream_response_first_byte",
	MiddleboxDownstreamResponseDone:   "middlebox_downstream_response_done",
	MiddleboxUpstreamConnectionBind:   "middlebox_upstream_connection_bind",

	CertServerRequest:          "certserver_request",
	CertServerGenerate:         "certserver_generate",
	CertServerGenerateDone:     "certserver_generate_done",
	CertServerResponse:         "certserver_response",
	CertServerError:            "certserver_error",
	CertServerQuoteVerify:      "certserver_quote_verify",
	CertServerQuoteDone:        "certserver_quote_done",
	CertServerRequestByID:      "certserver_request_by_id",
	CertServerGenerateByID:     "certserver_generate_by_id",
	CertServerGenerateDoneByID: "certserver_generate_done_by_id",
	CertServerResponseByID:     "certserver_response_by_id",
	CertServerErrorByID:        "certserver_error_by_id",
	CertServerQuoteVerifyByID:  "certserver_quote_verify_by_id",
	CertServerQuoteDoneByID:    "certserver_quote_done_by_id",

	RequestServerRequestStart:  "requestserver_request_start",
	RequestServerResponseStart: "requestserver_response_start",
	RequestServerResponseDone:  "requestserver_response_done",
}

func EventName(code uint32) string {
	if name, ok := eventNames[code]; ok {
		return name
	}
	return "unknown"
}
