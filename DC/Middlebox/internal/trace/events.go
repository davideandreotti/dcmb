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

	GatewayClientAccepted      uint32 = 2001
	GatewayQueueEnter          uint32 = 2002
	GatewayQueueLeave          uint32 = 2003
	GatewayQueueFull           uint32 = 2004
	GatewayQueueTimeout        uint32 = 2005
	GatewayWorkerSelectStart   uint32 = 2006
	GatewayWorkerSelectDone    uint32 = 2007
	GatewayBackendDialStart    uint32 = 2008
	GatewayBackendDialDone     uint32 = 2009
	GatewaySpliceStart         uint32 = 2010
	GatewaySpliceDone          uint32 = 2011
	GatewayRequestDropped      uint32 = 2012
	GatewayContainerCreate     uint32 = 2013
	GatewayContainerStartDone  uint32 = 2014
	GatewayContainerIPAssigned uint32 = 2015
	GatewayContainerReady      uint32 = 2016
	GatewayContainerRemove     uint32 = 2017
	GatewayContainerRemoved    uint32 = 2018

	MiddleboxTLSGetCertStart         uint32 = 3001
	MiddleboxTLSGetCertDone          uint32 = 3002
	MiddleboxDelegationCacheHit      uint32 = 3003
	MiddleboxDelegationMiss          uint32 = 3004
	MiddleboxDelegationFetch         uint32 = 3005
	MiddleboxDelegationFetched       uint32 = 3006
	MiddleboxAttestationStart        uint32 = 3007
	MiddleboxAttestationDone         uint32 = 3008
	MiddleboxValidationStart         uint32 = 3009
	MiddleboxValidationDone          uint32 = 3010
	MiddleboxRequestStart            uint32 = 3011
	MiddleboxRequestDone             uint32 = 3012
	MiddleboxUpstreamDialStart       uint32 = 3013
	MiddleboxUpstreamDialDone        uint32 = 3014
	MiddleboxUpstreamTLSStart        uint32 = 3015
	MiddleboxUpstreamTLSDone         uint32 = 3016
	MiddleboxProxyError              uint32 = 3017
	MiddleboxResponseValidationStart uint32 = 3018
	MiddleboxResponseValidationDone  uint32 = 3019

	CertServerRequest      uint32 = 4001
	CertServerGenerate     uint32 = 4002
	CertServerGenerateDone uint32 = 4003
	CertServerResponse     uint32 = 4004
	CertServerError        uint32 = 4005
	CertServerQuoteVerify  uint32 = 4006
	CertServerQuoteDone    uint32 = 4007
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

	GatewayClientAccepted:      "gateway_client_accepted",
	GatewayQueueEnter:          "gateway_queue_enter",
	GatewayQueueLeave:          "gateway_queue_leave",
	GatewayQueueFull:           "gateway_queue_full",
	GatewayQueueTimeout:        "gateway_queue_timeout",
	GatewayWorkerSelectStart:   "gateway_worker_select_start",
	GatewayWorkerSelectDone:    "gateway_worker_select_done",
	GatewayBackendDialStart:    "gateway_backend_dial_start",
	GatewayBackendDialDone:     "gateway_backend_dial_done",
	GatewaySpliceStart:         "gateway_splice_start",
	GatewaySpliceDone:          "gateway_splice_done",
	GatewayRequestDropped:      "gateway_request_dropped",
	GatewayContainerCreate:     "gateway_container_create",
	GatewayContainerStartDone:  "gateway_container_start_done",
	GatewayContainerIPAssigned: "gateway_container_ip_assigned",
	GatewayContainerReady:      "gateway_container_ready",
	GatewayContainerRemove:     "gateway_container_remove",
	GatewayContainerRemoved:    "gateway_container_removed",

	MiddleboxTLSGetCertStart:         "middlebox_tls_get_cert_start",
	MiddleboxTLSGetCertDone:          "middlebox_tls_get_cert_done",
	MiddleboxDelegationCacheHit:      "middlebox_delegation_cache_hit",
	MiddleboxDelegationMiss:          "middlebox_delegation_miss",
	MiddleboxDelegationFetch:         "middlebox_delegation_fetch",
	MiddleboxDelegationFetched:       "middlebox_delegation_fetched",
	MiddleboxAttestationStart:        "middlebox_attestation_start",
	MiddleboxAttestationDone:         "middlebox_attestation_done",
	MiddleboxValidationStart:         "middlebox_validation_start",
	MiddleboxValidationDone:          "middlebox_validation_done",
	MiddleboxRequestStart:            "middlebox_request_start",
	MiddleboxRequestDone:             "middlebox_request_done",
	MiddleboxUpstreamDialStart:       "middlebox_upstream_dial_start",
	MiddleboxUpstreamDialDone:        "middlebox_upstream_dial_done",
	MiddleboxUpstreamTLSStart:        "middlebox_upstream_tls_start",
	MiddleboxUpstreamTLSDone:         "middlebox_upstream_tls_done",
	MiddleboxProxyError:              "middlebox_proxy_error",
	MiddleboxResponseValidationStart: "middlebox_response_validation_start",
	MiddleboxResponseValidationDone:  "middlebox_response_validation_done",

	CertServerRequest:      "certserver_request",
	CertServerGenerate:     "certserver_generate",
	CertServerGenerateDone: "certserver_generate_done",
	CertServerResponse:     "certserver_response",
	CertServerError:        "certserver_error",
	CertServerQuoteVerify:  "certserver_quote_verify",
	CertServerQuoteDone:    "certserver_quote_done",
}

func EventName(code uint32) string {
	if name, ok := eventNames[code]; ok {
		return name
	}
	return "unknown"
}
