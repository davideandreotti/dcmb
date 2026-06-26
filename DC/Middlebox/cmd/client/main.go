// client.go
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	benchtrace "dc/middlebox/internal/trace"
)

type headerList []string

const defaultCA = "/home/bonsai/dcmb/certs_external/ca.crt"

var logLevel = "error"

func info(msg string) {
	if logLevel != "debug" {
		return
	}
	fmt.Fprintln(os.Stderr, msg)
}

func (h *headerList) String() string {
	return strings.Join(*h, ",")
}

func (h *headerList) Set(value string) error {
	*h = append(*h, value)
	return nil
}

func errorArg(err error) uint64 {
	if err != nil {
		return 1
	}
	return 0
}

func resolveClientID(explicit string) string {
	if explicit != "" {
		return explicit
	}

	if envID := strings.TrimSpace(os.Getenv("CLIENT_ID")); envID != "" {
		return envID
	}

	host, err := os.Hostname()
	if err == nil {
		return host
	}

	return "unknown-client"
}

func serverNameForURL(rawURL string, serverNameOverride string) (string, error) {
	parsedURL, err := url.Parse(rawURL)
	if err != nil {
		return "", err
	}

	if serverNameOverride != "" {
		return serverNameOverride, nil
	}
	return parsedURL.Hostname(), nil
}

func newHTTPClient(caPath string, serverName string, closeAfterRequest bool) (*http.Client, *http.Transport, error) {
	caPEM, err := os.ReadFile(caPath)
	if err != nil {
		return nil, nil, err
	}

	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, nil, fmt.Errorf("unable to parse CA certificate")
	}

	tr := &http.Transport{
		DisableKeepAlives: closeAfterRequest,
		TLSClientConfig: &tls.Config{
			RootCAs:                    roots,
			ServerName:                 serverName,
			MinVersion:                 tls.VersionTLS13,
			SupportDelegatedCredential: true,
			VerifyPeerCertificate: func(rawCerts [][]byte, verifiedChains [][]*x509.Certificate) error {
				info(fmt.Sprintf("t23: [CLIENT] - CertValidationStart = %d ns", time.Now().UnixNano()))

				if len(verifiedChains) == 0 {
					return fmt.Errorf("certificate verification failed")
				}

				info(fmt.Sprintf("t24: [CLIENT] - CertValidationDone = %d ns", time.Now().UnixNano()))
				return nil
			},
		},
	}

	return &http.Client{Transport: tr}, tr, nil
}

func doHTTPSRequest(
	client *http.Client,
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	traceID string,
	closeAfterRequest bool,
) ([]byte, int, error) {
	var reqBody io.Reader
	if body != "" {
		reqBody = strings.NewReader(body)
	}

	req, err := http.NewRequest(method, rawURL, reqBody)
	if err != nil {
		return nil, 0, err
	}

	for _, header := range headers {
		parts := strings.SplitN(header, ":", 2)
		if len(parts) != 2 {
			return nil, 0, fmt.Errorf("invalid header: %s", header)
		}

		req.Header.Add(strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]))
	}

	req.Header.Set("X-Client-ID", clientID)
	if traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	req.Close = closeAfterRequest

	trace := &httptrace.ClientTrace{
		ConnectStart: func(network string, addr string) {
			benchtrace.Mark(benchtrace.ClientTCPConnectStart, traceID, 0)
			info(fmt.Sprintf("t27: [CLIENT] - TCPConnectStart = %d ns", time.Now().UnixNano()))
		},
		ConnectDone: func(network string, addr string, err error) {
			benchtrace.Mark(benchtrace.ClientTCPConnectDone, traceID, errorArg(err))
			info(fmt.Sprintf("t28: [CLIENT] - TCPConnectDone = %d ns", time.Now().UnixNano()))
		},
		TLSHandshakeStart: func() {
			benchtrace.Mark(benchtrace.ClientTLSStart, traceID, 0)
			info(fmt.Sprintf("t20: [CLIENT] - TLSHandshakeStart = %d ns", time.Now().UnixNano()))
		},
		TLSHandshakeDone: func(cs tls.ConnectionState, err error) {
			benchtrace.Mark(benchtrace.ClientTLSDone, traceID, errorArg(err))
			if err != nil {
				info(fmt.Sprintf("t21: [CLIENT] - TLSHandshakeDoneError = %d ns err=%v", time.Now().UnixNano(), err))
				return
			}

			info(fmt.Sprintf("t21: [CLIENT] - TLSHandshakeDone = %d ns", time.Now().UnixNano()))
		},
		WroteRequest: func(info httptrace.WroteRequestInfo) {
			benchtrace.Mark(benchtrace.ClientRequestSent, traceID, errorArg(info.Err))
		},
		GotFirstResponseByte: func() {
			benchtrace.Mark(benchtrace.ClientResponseFirst, traceID, 0)
		},
	}

	req = req.WithContext(httptrace.WithClientTrace(context.Background(), trace))

	info(fmt.Sprintf("t1: [CLIENT] - ClientHello = %d ns", time.Now().UnixNano()))

	resp, err := client.Do(req)
	if err != nil {
		benchtrace.Mark(benchtrace.ClientRequestError, traceID, 1)
		return nil, 0, err
	}
	defer resp.Body.Close()

	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		benchtrace.Mark(benchtrace.ClientRequestError, traceID, 2)
		return nil, resp.StatusCode, err
	}

	benchtrace.Mark(benchtrace.ClientResponseDone, traceID, uint64(resp.StatusCode))
	info(fmt.Sprintf("t10: [CLIENT] - ServerHelloLatency = %d ns", time.Now().UnixNano()))

	return responseBody, resp.StatusCode, nil
}

func httpsClient(
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverNameOverride string,
	traceID string,
) ([]byte, int, error) {
	serverName, err := serverNameForURL(rawURL, serverNameOverride)
	if err != nil {
		return nil, 0, err
	}

	client, tr, err := newHTTPClient(caPath, serverName, true)
	if err != nil {
		return nil, 0, err
	}
	defer tr.CloseIdleConnections()

	return doHTTPSRequest(client, method, rawURL, headers, body, clientID, traceID, true)
}

func runRequest(
	id int,
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverName string,
) (int, time.Duration, error) {
	info(fmt.Sprintf("Calling request %d: %s as client_id=%s", id, rawURL, clientID))

	traceID := fmt.Sprintf("%s-%d", clientID, id)
	benchtrace.Mark(benchtrace.ClientRequestStart, traceID, uint64(id))
	start := time.Now()
	response, status, err := httpsClient(method, rawURL, headers, body, clientID, caPath, serverName, traceID)
	latency := time.Since(start)
	if err != nil {
		return status, latency, err
	}

	if status < 200 || status >= 300 {
		log.Printf("Request %d returned non-2xx HTTP status %d", id, status)
		log.Println("Response: ", string(response))
	}

	info(string(response))
	return status, latency, nil
}

func runIsolatedWarmupRequests(
	count int,
	wait time.Duration,
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverName string,
) error {
	for i := 1; i <= count; i++ {
		traceID := fmt.Sprintf("warmup-%d", i)
		info(fmt.Sprintf("Calling warmup request %d: %s as client_id=%s", i, rawURL, clientID))
		benchtrace.Mark(benchtrace.ClientRequestStart, traceID, uint64(i))

		response, status, err := httpsClient(method, rawURL, headers, body, clientID, caPath, serverName, traceID)
		if err != nil {
			return fmt.Errorf("warmup request %d failed: %w", i, err)
		}
		if status < 200 || status >= 300 {
			log.Printf("Warmup request %d returned non-2xx HTTP status %d", i, status)
			log.Println("Response: ", string(response))
			return fmt.Errorf("warmup request %d returned non-2xx HTTP status %d", i, status)
		}
	}

	if count > 0 && wait > 0 {
		time.Sleep(wait)
	}
	return nil
}

func handleRequestError(requestID int, err error, continueOnError bool) error {
	if err == nil {
		return nil
	}

	log.Printf("Request %d failed: %v", requestID, err)

	if continueOnError {
		return nil
	}

	return err
}

func runLoopFixedRate(
	duration time.Duration,
	rateRPS float64,
	continueOnError bool,
	maxInFlight int,
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverName string,
) error {
	if rateRPS <= 0 {
		return fmt.Errorf("rate must be > 0")
	}

	rate := time.Duration(float64(time.Second) / rateRPS)
	if rate <= 0 {
		rate = time.Nanosecond
	}

	end := time.Now().Add(duration)
	next := time.Now().Add(rate)

	requestID := 1

	sem := make(chan struct{}, maxInFlight)
	var wg sync.WaitGroup

	errCh := make(chan error, 1)
	var stop atomic.Bool

	var totalScheduled atomic.Int64
	var totalStarted atomic.Int64
	var totalLate atomic.Int64
	var totalSuccess atomic.Int64 // risposte 2xx
	var totalNon2xx atomic.Int64  // status code != 2xx
	var totalErrors atomic.Int64  // errori di trasporto/timeout/lettura

	// Latenze (in ns) raccolte per p50/p95/p99 a fine run.
	// Protetto da mutex; per rate molto alti puoi sostituire con un
	// HDR histogram (es. codahale/hdrhistogram) per evitare contesa.
	var latMu sync.Mutex
	latencies := make([]int64, 0, 1024)
	loopStart := time.Now()

	info(fmt.Sprintf("Fixed-rate loop started: duration=%v offered_rate=%.4f req/s interval=%v maxInFlight=%d", duration, rateRPS, rate, maxInFlight))

	for {
		if stop.Load() {
			break
		}

		now := time.Now()
		if now.After(end) {
			break
		}

		if now.Before(next) {

			time.Sleep(next.Sub(now))
		} else if now.Sub(next) >= rate {
			/*
				In ritardo: la scadenza di uno o più slot è già passata.

				Policy: SKIP totale degli slot persi, nessuna richiesta
				di "recupero" viene emessa. Riallineiamo 'next' al primo
				slot futuro e saltiamo questa iterazione del loop.

				Conteggio: ogni slot la cui scadenza ideale e' <= now
				e' considerato perso, incluso lo slot 'next' su cui
				siamo gia' in ritardo.

				Esempio con rate=50ms:
				  next = 1000, now = 1230
				  slot scaduti: 1000, 1050, 1100, 1150, 1200  -> missed = 5
				  nuovo next (primo slot futuro)              = 1250
			*/
			missed := int64(now.Sub(next)/rate) + 1
			totalLate.Add(missed)
			log.Printf("LATE!! missed_slots=%d (drift=%v) -> realign next to first future slot",
				missed, now.Sub(next))
			next = next.Add(time.Duration(missed) * rate)
			continue
		}
		// else: now == next oppure 0 < now-next < rate (jitter accettabile) -> in fase.

		scheduled := next
		next = next.Add(rate)

		id := requestID
		requestID++
		totalScheduled.Add(1)
		benchtrace.Mark(benchtrace.ClientRequestScheduled, fmt.Sprintf("%s-%d", clientID, id), uint64(id))

		/*
			Semaforo bloccante.

			Se ci sono già maxInFlight richieste attive, questa riga blocca
			il loop prima di creare una nuova goroutine.

			Quindi:
			- non vengono droppate richieste;
			- non vengono create goroutine infinite;
			- il producer resta indietro se il sistema non riesce a sostenere il rate.
		*/
		sem <- struct{}{}

		wg.Add(1)
		totalStarted.Add(1)

		go func(reqID int, scheduledAt time.Time) {
			defer wg.Done()
			defer func() {
				<-sem
			}()

			//actualStart := time.Now()

			// log.Printf(
			// 	"Request %d scheduled_at=%d actual_start=%d scheduling_delay_ns=%d",
			// 	reqID,
			// 	scheduledAt.UnixNano(),
			// 	actualStart.UnixNano(),
			// 	actualStart.Sub(scheduledAt).Nanoseconds(),
			// )

			status, latency, err := runRequest(reqID, method, rawURL, headers, body, clientID, caPath, serverName)
			_ = scheduledAt

			switch {
			case err != nil:
				totalErrors.Add(1)
			case status >= 200 && status < 300:
				totalSuccess.Add(1)
				latMu.Lock()
				latencies = append(latencies, latency.Nanoseconds())
				latMu.Unlock()
			default:
				totalNon2xx.Add(1)
			}

			if handledErr := handleRequestError(reqID, err, continueOnError); handledErr != nil {
				if stop.CompareAndSwap(false, true) {
					select {
					case errCh <- handledErr:
					default:
					}
				}
			}
		}(id, scheduled)
	}

	info("Fixed-rate loop ended: waiting for active requests...")
	wg.Wait()
	elapsed := time.Since(loopStart)

	select {
	case err := <-errCh:
		return err
	default:
	}

	// === Throughput report (offered vs achieved) ===
	offeredRate := rateRPS // req/s richiesti
	scheduledRate := float64(totalScheduled.Load()) / elapsed.Seconds()
	startedRate := float64(totalStarted.Load()) / elapsed.Seconds()
	achievedRate := float64(totalSuccess.Load()) / elapsed.Seconds()

	latMu.Lock()
	p50, p95, p99, mean := percentiles(latencies)
	nSamples := len(latencies)
	latMu.Unlock()

	saturationRatio := 0.0
	if offeredRate > 0 {
		saturationRatio = achievedRate / offeredRate
	}

	log.Println("=== THROUGHPUT REPORT ===")
	log.Printf("  elapsed              = %v", elapsed)
	log.Printf("  offered_rate_rps     = %.2f   (target from -rate)", offeredRate)
	log.Printf("  scheduled_rate_rps   = %.2f   (slots actually planned)", scheduledRate)
	log.Printf("  started_rate_rps     = %.2f   (post-semaphore, sent to server)", startedRate)
	log.Printf("  achieved_rate_rps    = %.2f   (2xx responses received)", achievedRate)
	log.Printf("  saturation_ratio     = %.3f   (achieved/offered; <1 => system saturated)", saturationRatio)
	log.Printf("  totals: scheduled=%d started=%d success=%d non2xx=%d errors=%d late_slots=%d",
		totalScheduled.Load(), totalStarted.Load(),
		totalSuccess.Load(), totalNon2xx.Load(), totalErrors.Load(),
		totalLate.Load(),
	)
	if nSamples > 0 {
		log.Printf("  latency (n=%d): mean=%v p50=%v p95=%v p99=%v", nSamples, mean, p50, p95, p99)
	} else {
		log.Println("  latency: no successful samples")
	}

	// Riga "machine-readable" pensata per essere parsata dagli script Python.
	fmt.Printf("THROUGHPUT_CSV,offered_rps,scheduled_rps,started_rps,achieved_rps,sat_ratio,success,non2xx,errors,late,p50_ns,p95_ns,p99_ns,mean_ns,elapsed_s\n")
	fmt.Printf("THROUGHPUT_DATA,%.4f,%.4f,%.4f,%.4f,%.4f,%d,%d,%d,%d,%d,%d,%d,%d,%.4f\n",
		offeredRate, scheduledRate, startedRate, achievedRate, saturationRatio,
		totalSuccess.Load(), totalNon2xx.Load(), totalErrors.Load(),
		totalLate.Load(),
		p50.Nanoseconds(), p95.Nanoseconds(), p99.Nanoseconds(), mean.Nanoseconds(),
		elapsed.Seconds(),
	)

	return nil
}

func runLoopPersistentClients(
	duration time.Duration,
	rateRPS float64,
	clients int,
	requestsPerClient int,
	continueOnError bool,
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverNameOverride string,
) error {
	if clients <= 0 {
		return fmt.Errorf("clients must be > 0")
	}
	if duration <= 0 && requestsPerClient <= 0 {
		return fmt.Errorf("persistent mode requires -d > 0 or -requests-per-client > 0")
	}

	serverName, err := serverNameForURL(rawURL, serverNameOverride)
	if err != nil {
		return err
	}

	end := time.Now().Add(duration)
	loopStart := time.Now()
	closedLoop := rateRPS <= 0
	globalInterval := time.Duration(0)
	perClientRate := 0.0
	perClientInterval := time.Duration(0)
	if !closedLoop {
		globalInterval = time.Duration(float64(time.Second) / rateRPS)
		if globalInterval <= 0 {
			globalInterval = time.Nanosecond
		}
		perClientRate = rateRPS / float64(clients)
		perClientInterval = time.Duration(float64(time.Second) / perClientRate)
		if perClientInterval <= 0 {
			perClientInterval = time.Nanosecond
		}
	}

	if closedLoop {
		info(fmt.Sprintf("Persistent closed-loop started: duration=%v clients=%d requests_per_client=%d", duration, clients, requestsPerClient))
	} else {
		info(fmt.Sprintf("Persistent loop started: duration=%v total_rate=%.4f req/s clients=%d per_client_rate=%.4f req/s per_client_interval=%v requests_per_client=%d", duration, rateRPS, clients, perClientRate, perClientInterval, requestsPerClient))
	}

	errCh := make(chan error, 1)
	var stop atomic.Bool
	var wg sync.WaitGroup
	var nextRequestID atomic.Int64

	var totalScheduled atomic.Int64
	var totalStarted atomic.Int64
	var totalLate atomic.Int64
	var totalSuccess atomic.Int64
	var totalNon2xx atomic.Int64
	var totalErrors atomic.Int64

	var latMu sync.Mutex
	allLatencies := make([]int64, 0, 1024)
	firstLatencies := make([]int64, 0, clients)
	reusedLatencies := make([]int64, 0, 1024)

	for clientIndex := 0; clientIndex < clients; clientIndex++ {
		wg.Add(1)
		go func(clientIndex int) {
			defer wg.Done()

			localClientID := fmt.Sprintf("%s-%d", clientID, clientIndex+1)
			httpClient, transport, err := newHTTPClient(caPath, serverName, false)
			if err != nil {
				totalErrors.Add(1)
				if stop.CompareAndSwap(false, true) {
					select {
					case errCh <- err:
					default:
					}
				}
				return
			}
			defer transport.CloseIdleConnections()

			next := loopStart
			if !closedLoop {
				next = loopStart.Add(time.Duration(clientIndex) * globalInterval)
			}
			for localReq := 0; ; {
				if stop.Load() {
					return
				}
				if requestsPerClient > 0 && localReq >= requestsPerClient {
					return
				}
				if requestsPerClient <= 0 && duration > 0 && !time.Now().Before(end) {
					return
				}

				if !closedLoop {
					now := time.Now()
					if now.Before(next) {
						time.Sleep(next.Sub(now))
					} else if now.Sub(next) >= perClientInterval {
						missed := int64(now.Sub(next)/perClientInterval) + 1
						totalLate.Add(missed)
						next = next.Add(time.Duration(missed) * perClientInterval)
						continue
					}
					next = next.Add(perClientInterval)
				}

				reqID := int(nextRequestID.Add(1))
				totalScheduled.Add(1)
				totalStarted.Add(1)
				traceID := fmt.Sprintf("%s-%d", localClientID, reqID)
				benchtrace.Mark(benchtrace.ClientRequestScheduled, traceID, uint64(reqID))

				info(fmt.Sprintf("Calling request %d: %s as client_id=%s", reqID, rawURL, localClientID))
				benchtrace.Mark(benchtrace.ClientRequestStart, traceID, uint64(reqID))
				start := time.Now()
				response, status, err := doHTTPSRequest(httpClient, method, rawURL, headers, body, localClientID, traceID, false)
				latency := time.Since(start)

				switch {
				case err != nil:
					totalErrors.Add(1)
				case status >= 200 && status < 300:
					totalSuccess.Add(1)
					latMu.Lock()
					allLatencies = append(allLatencies, latency.Nanoseconds())
					if localReq == 0 {
						firstLatencies = append(firstLatencies, latency.Nanoseconds())
					} else {
						reusedLatencies = append(reusedLatencies, latency.Nanoseconds())
					}
					latMu.Unlock()
					info(string(response))
				default:
					totalNon2xx.Add(1)
					log.Printf("Request %d returned non-2xx HTTP status %d", reqID, status)
					log.Println("Response: ", string(response))
				}

				localReq++

				if handledErr := handleRequestError(reqID, err, continueOnError); handledErr != nil {
					if stop.CompareAndSwap(false, true) {
						select {
						case errCh <- handledErr:
						default:
						}
					}
					return
				}
			}
		}(clientIndex)
	}

	wg.Wait()
	elapsed := time.Since(loopStart)

	select {
	case err := <-errCh:
		return err
	default:
	}

	scheduledRate := float64(totalScheduled.Load()) / elapsed.Seconds()
	startedRate := float64(totalStarted.Load()) / elapsed.Seconds()
	achievedRate := float64(totalSuccess.Load()) / elapsed.Seconds()

	latMu.Lock()
	p50, p95, p99, mean := percentiles(allLatencies)
	firstP50, firstP95, firstP99, firstMean := percentiles(firstLatencies)
	reusedP50, reusedP95, reusedP99, reusedMean := percentiles(reusedLatencies)
	nSamples := len(allLatencies)
	nFirst := len(firstLatencies)
	nReused := len(reusedLatencies)
	latMu.Unlock()

	offeredRate := rateRPS
	if closedLoop {
		offeredRate = achievedRate
	}

	saturationRatio := 0.0
	if offeredRate > 0 {
		saturationRatio = achievedRate / offeredRate
	}

	log.Println("=== THROUGHPUT REPORT ===")
	log.Printf("  mode                 = persistent")
	log.Printf("  clients              = %d", clients)
	log.Printf("  elapsed              = %v", elapsed)
	if closedLoop {
		log.Printf("  offered_rate_rps     = closed-loop   (no -rate limit)")
	} else {
		log.Printf("  offered_rate_rps     = %.2f   (total target from -rate)", offeredRate)
	}
	log.Printf("  scheduled_rate_rps   = %.2f   (slots actually planned)", scheduledRate)
	log.Printf("  started_rate_rps     = %.2f   (sent to server)", startedRate)
	log.Printf("  achieved_rate_rps    = %.2f   (2xx responses received)", achievedRate)
	log.Printf("  saturation_ratio     = %.3f   (achieved/offered; <1 => system saturated)", saturationRatio)
	log.Printf("  totals: scheduled=%d started=%d success=%d non2xx=%d errors=%d late_slots=%d",
		totalScheduled.Load(), totalStarted.Load(),
		totalSuccess.Load(), totalNon2xx.Load(), totalErrors.Load(),
		totalLate.Load(),
	)
	if nSamples > 0 {
		log.Printf("  latency all (n=%d): mean=%v p50=%v p95=%v p99=%v", nSamples, mean, p50, p95, p99)
	} else {
		log.Println("  latency all: no successful samples")
	}
	if nFirst > 0 {
		log.Printf("  latency first_request (n=%d): mean=%v p50=%v p95=%v p99=%v", nFirst, firstMean, firstP50, firstP95, firstP99)
	} else {
		log.Println("  latency first_request: no successful samples")
	}
	if nReused > 0 {
		log.Printf("  latency reused_connection (n=%d): mean=%v p50=%v p95=%v p99=%v", nReused, reusedMean, reusedP50, reusedP95, reusedP99)
	} else {
		log.Println("  latency reused_connection: no successful samples")
	}

	fmt.Printf("THROUGHPUT_CSV,mode,offered_rps,scheduled_rps,started_rps,achieved_rps,sat_ratio,success,non2xx,errors,late,all_p50_ns,all_p95_ns,all_p99_ns,all_mean_ns,first_n,first_p50_ns,first_p95_ns,first_p99_ns,first_mean_ns,reused_n,reused_p50_ns,reused_p95_ns,reused_p99_ns,reused_mean_ns,elapsed_s\n")
	fmt.Printf("THROUGHPUT_DATA,persistent,%.4f,%.4f,%.4f,%.4f,%.4f,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%.4f\n",
		offeredRate, scheduledRate, startedRate, achievedRate, saturationRatio,
		totalSuccess.Load(), totalNon2xx.Load(), totalErrors.Load(), totalLate.Load(),
		p50.Nanoseconds(), p95.Nanoseconds(), p99.Nanoseconds(), mean.Nanoseconds(),
		nFirst, firstP50.Nanoseconds(), firstP95.Nanoseconds(), firstP99.Nanoseconds(), firstMean.Nanoseconds(),
		nReused, reusedP50.Nanoseconds(), reusedP95.Nanoseconds(), reusedP99.Nanoseconds(), reusedMean.Nanoseconds(),
		elapsed.Seconds(),
	)

	return nil
}

// TODO Controllare se ci interessa contare i percentili e le stats in questo codice
// percentiles ritorna p50, p95, p99 e media di una slice di durate in ns.
// Slice viene ordinata in-place.
func percentiles(ns []int64) (p50, p95, p99, mean time.Duration) {
	if len(ns) == 0 {
		return 0, 0, 0, 0
	}
	sort.Slice(ns, func(i, j int) bool { return ns[i] < ns[j] })
	pick := func(p float64) time.Duration {
		idx := int(float64(len(ns)-1) * p)
		return time.Duration(ns[idx])
	}
	var sum int64
	for _, v := range ns {
		sum += v
	}
	return pick(0.50), pick(0.95), pick(0.99), time.Duration(sum / int64(len(ns)))
}

func main() {
	var headers headerList

	clientIDFlag := flag.String("id", "", "Client identifier")
	dataFlag := flag.String("data", "", "POST body")

	caPathFlag := flag.String("ca", defaultCA, "CA certificate")
	serverNameFlag := flag.String("servername", "", "TLS ServerName")

	durationFlag := flag.Int("d", 0, "duration in seconds")
	rateFlag := flag.Float64("rate", 0, "total offered request rate in requests per second")
	modeFlag := flag.String("mode", "fresh", "experiment mode: fresh or persistent")
	clientsFlag := flag.Int("clients", 1, "number of persistent client goroutines")
	requestsPerClientFlag := flag.Int("requests-per-client", 0, "persistent requests per client; 0 means run for duration")

	continueFlag := flag.Bool("continue-on-error", true, "continue if request fails")
	logLevelFlag := flag.String("log_level", "error", "log level: error or debug")
	tracePathFlag := flag.String("trace", "", "trace output file; requires build tag trace")
	traceBufferFlag := flag.Int("trace-buffer-events", 100000, "trace buffer capacity in events")
	traceDropFlag := flag.Bool("trace-drop-on-full", true, "drop trace events instead of blocking when trace buffer is full")

	maxInFlightFlag := flag.Int(
		"max-in-flight",
		64,
		"maximum number of concurrent in-flight requests",
	)

	flag.Var(&headers, "H", "Header: Key: Value")
	flag.Parse()

	logLevel = strings.ToLower(strings.TrimSpace(*logLevelFlag))
	if logLevel != "error" && logLevel != "debug" {
		log.Fatalf("invalid -log_level %q: expected error or debug", *logLevelFlag)
	}
	if err := benchtrace.Start(*tracePathFlag, *traceBufferFlag, *traceDropFlag); err != nil {
		log.Fatalf("trace start: %v", err)
	}
	defer benchtrace.Stop()

	if len(flag.Args()) < 1 {
		fmt.Println("Usage:")
		fmt.Println("  ./client -H \"Authorization: Bearer token\" https://server:8443/function/init")
		fmt.Println("  ./client -log_level debug -d 30 -rate 10 -max-in-flight 32 -H \"Authorization: Bearer token\" https://server:8443/function/init")
		fmt.Println("  ./client -mode persistent -clients 8 -d 30 -rate 40 -H \"Authorization: Bearer token\" https://server:8443/function/init")
		fmt.Println("  ./client -mode persistent -clients 8 -d 30 -H \"Authorization: Bearer token\" https://server:8443/function/init")
		os.Exit(1)
	}

	rawURL := flag.Args()[0]

	method := "GET"
	if *dataFlag != "" {
		method = "POST"
	}

	clientID := resolveClientID(*clientIDFlag)

	mode := strings.ToLower(strings.TrimSpace(*modeFlag))
	if mode == "" {
		mode = "fresh"
	}

	if mode == "fresh" && (*durationFlag <= 0 || *rateFlag <= 0) {
		_, _, err := runRequest(
			1,
			method,
			rawURL,
			headers,
			*dataFlag,
			clientID,
			*caPathFlag,
			*serverNameFlag,
		)

		if err != nil {
			panic(err)
		}

		return
	}

	if mode != "persistent" && mode != "session" && mode != "keepalive" && *rateFlag <= 0 {
		panic("rate must be > 0")
	}

	if *maxInFlightFlag <= 0 {
		panic("max-in-flight must be > 0")
	}

	if err := runIsolatedWarmupRequests(
		1,
		time.Second,
		method,
		rawURL,
		headers,
		*dataFlag,
		clientID,
		*caPathFlag,
		*serverNameFlag,
	); err != nil {
		panic(err)
	}

	duration := time.Duration(*durationFlag) * time.Second
	var err error
	switch mode {
	case "fresh", "single", "one-shot":
		err = runLoopFixedRate(
			duration,
			*rateFlag,
			*continueFlag,
			*maxInFlightFlag,
			method,
			rawURL,
			headers,
			*dataFlag,
			clientID,
			*caPathFlag,
			*serverNameFlag,
		)
	case "persistent", "session", "keepalive":
		err = runLoopPersistentClients(
			duration,
			*rateFlag,
			*clientsFlag,
			*requestsPerClientFlag,
			*continueFlag,
			method,
			rawURL,
			headers,
			*dataFlag,
			clientID,
			*caPathFlag,
			*serverNameFlag,
		)
	default:
		panic("unsupported mode: " + *modeFlag)
	}

	if err != nil {
		panic(err)
	}
}

// INCONTRO TESI 29 MAGGIO

// per esempio sto bene a 10ms di latenza: fisso 20/25 ms di latenza e aumento il rate finché non vedo che la latenza media supera i 20ms, a quel punto so che sono al limite e posso fare un'analisi più dettagliata sui percentili. Se invece fisso un rate troppo alto fin da subito rischio di saturare il sistema e non capire qual è la capacità effettiva.
// a quella soglia corrisponde throghput: ritardo medio o numero richieste inevase
// loss > % -> sistema saturo, non riesce a sostenere il rate richiesto (throughput massimo)

// inutile misurare latenza se c'è coda di inevase perchè stai misurando solo sulle prime

//
