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
)

type headerList []string

const defaultCA = "/home/bonsai/Desktop/MasterThesis/certs_external/ca.crt"

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

func httpsClient(
	method string,
	rawURL string,
	headers []string,
	body string,
	clientID string,
	caPath string,
	serverNameOverride string,
) ([]byte, int, error) {
	caPEM, err := os.ReadFile(caPath)
	if err != nil {
		return nil, 0, err
	}

	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, 0, fmt.Errorf("unable to parse CA certificate")
	}

	parsedURL, err := url.Parse(rawURL)
	if err != nil {
		return nil, 0, err
	}

	serverName := serverNameOverride
	if serverName == "" {
		serverName = parsedURL.Hostname()
	}

	tr := &http.Transport{
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

	client := &http.Client{Transport: tr}

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

	trace := &httptrace.ClientTrace{
		ConnectStart: func(network string, addr string) {
			info(fmt.Sprintf("t27: [CLIENT] - TCPConnectStart = %d ns", time.Now().UnixNano()))
		},
		ConnectDone: func(network string, addr string, err error) {
			info(fmt.Sprintf("t28: [CLIENT] - TCPConnectDone = %d ns", time.Now().UnixNano()))
		},
		TLSHandshakeStart: func() {
			info(fmt.Sprintf("t20: [CLIENT] - TLSHandshakeStart = %d ns", time.Now().UnixNano()))
		},
		TLSHandshakeDone: func(cs tls.ConnectionState, err error) {
			if err != nil {
				info(fmt.Sprintf("t21: [CLIENT] - TLSHandshakeDoneError = %d ns err=%v", time.Now().UnixNano(), err))
				return
			}

			info(fmt.Sprintf("t21: [CLIENT] - TLSHandshakeDone = %d ns", time.Now().UnixNano()))
		},
	}

	req = req.WithContext(httptrace.WithClientTrace(context.Background(), trace))

	info(fmt.Sprintf("t1: [CLIENT] - ClientHello = %d ns", time.Now().UnixNano()))

	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}

	info(fmt.Sprintf("t10: [CLIENT] - ServerHelloLatency = %d ns", time.Now().UnixNano()))

	return responseBody, resp.StatusCode, nil
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

	start := time.Now()
	response, status, err := httpsClient(method, rawURL, headers, body, clientID, caPath, serverName)
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
	rate time.Duration,
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

	info(fmt.Sprintf("Fixed-rate loop started: duration=%v rate=%v maxInFlight=%d", duration, rate, maxInFlight))

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
	offeredRate := float64(time.Second) / float64(rate) // req/s richiesti
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
	rateFlag := flag.Int("rate", 0, "interval between requests in milliseconds")

	continueFlag := flag.Bool("continue-on-error", true, "continue if request fails")
	logLevelFlag := flag.String("log_level", "error", "log level: error or debug")

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

	if len(flag.Args()) < 1 {
		fmt.Println("Usage:")
		fmt.Println("  ./client -H \"Authorization: Bearer token\" https://server:8443/function/init")
		fmt.Println("  ./client -log_level debug -d 30 -rate 50 -max-in-flight 32 -H \"Authorization: Bearer token\" https://server:8443/function/init")
		os.Exit(1)
	}

	rawURL := flag.Args()[0]

	method := "GET"
	if *dataFlag != "" {
		method = "POST"
	}

	clientID := resolveClientID(*clientIDFlag)

	if *durationFlag <= 0 || *rateFlag <= 0 {
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

	if *maxInFlightFlag <= 0 {
		panic("max-in-flight must be > 0")
	}

	duration := time.Duration(*durationFlag) * time.Second
	rate := time.Duration(*rateFlag) * time.Millisecond

	err := runLoopFixedRate(
		duration,
		rate,
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
