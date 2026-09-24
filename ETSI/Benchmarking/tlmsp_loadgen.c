#define _POSIX_C_SOURCE 200809L

#include <curl/curl.h>

#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define DEFAULT_URL "https://127.0.0.1:4444/function/init"
#define DEFAULT_CONFIG "/home/bonsai/dcmb/ETSI/Configurations/local_init.ucl"
#define NS_PER_SECOND UINT64_C(1000000000)

enum benchmark_mode {
	MODE_PERSISTENT,
	MODE_FRESH
};

struct response_buffer {
	char *data;
	size_t length;
	size_t capacity;
};

struct options {
	enum benchmark_mode mode;
	const char *url;
	const char *config;
	unsigned int warmup;
	unsigned int requests;
	unsigned int interval_ms;
	unsigned int warmup_pause_ms;
	unsigned int max_in_flight;
	double duration_seconds;
	double rate;
	bool duration_mode;
};

struct counters {
	uint64_t transfer;
	uint64_t scheduled;
	uint64_t launched;
	uint64_t completed;
	uint64_t successes;
	uint64_t errors;
	uint64_t missed;
	unsigned int active;
	unsigned int max_active;
};

struct fresh_transfer {
	CURL *easy;
	struct response_buffer response;
	uint64_t transfer;
	uint64_t sample;
	uint64_t scheduled;
};

static void
usage(const char *program)
{
	fprintf(stderr,
	    "Usage: %s [--mode persistent|fresh] [--url URL] [--config FILE]\n"
	    "       [--warmup N] [--warmup-pause-ms N] [--max-in-flight N]\n"
	    "       ([--requests N] [--interval-ms N] | "
	    "--duration-seconds S --rate R)\n", program);
}

static bool
parse_nonnegative(const char *text, unsigned int *value)
{
	char *end;
	unsigned long parsed;

	errno = 0;
	parsed = strtoul(text, &end, 10);
	if (errno != 0 || text[0] == '\0' || *end != '\0' ||
	    parsed > 1000000000UL)
		return (false);
	*value = (unsigned int)parsed;
	return (true);
}

static bool
parse_positive_double(const char *text, double *value)
{
	char *end;
	double parsed;

	errno = 0;
	parsed = strtod(text, &end);
	if (errno != 0 || text[0] == '\0' || *end != '\0' || parsed <= 0.0)
		return (false);
	*value = parsed;
	return (true);
}

static uint64_t
wallclock_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_REALTIME, &ts) != 0)
		return (0);
	return ((uint64_t)ts.tv_sec * NS_PER_SECOND + (uint64_t)ts.tv_nsec);
}

static int
sleep_until_ns(uint64_t deadline_ns)
{
	struct timespec deadline;
	int result;

	deadline.tv_sec = (time_t)(deadline_ns / NS_PER_SECOND);
	deadline.tv_nsec = (long)(deadline_ns % NS_PER_SECOND);
	do {
		result = clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME,
		    &deadline, NULL);
	} while (result == EINTR);
	return (result);
}

static size_t
capture_body(char *data, size_t size, size_t count, void *arg)
{
	struct response_buffer *buffer = arg;
	size_t bytes = size * count;
	size_t required = buffer->length + bytes + 1;
	char *replacement;

	if (required > buffer->capacity) {
		size_t capacity = required < 256 ? 256 : required * 2;
		replacement = realloc(buffer->data, capacity);
		if (replacement == NULL)
			return (0);
		buffer->data = replacement;
		buffer->capacity = capacity;
	}
	memcpy(buffer->data + buffer->length, data, bytes);
	buffer->length += bytes;
	buffer->data[buffer->length] = '\0';
	return (bytes);
}

static void
reset_response(struct response_buffer *response)
{
	response->length = 0;
	if (response->data != NULL)
		response->data[0] = '\0';
}

static bool
response_is_expected(const struct response_buffer *response)
{
	return (response->data != NULL &&
	    strstr(response->data, "status") != NULL &&
	    strstr(response->data, "ok") != NULL &&
	    strstr(response->data, "function initialized") != NULL);
}

static bool
set_common_options(CURL *easy, const struct options *options,
    struct curl_slist *headers, struct response_buffer *response)
{
#define SETOPT(option, value) do {                                      \
	if (curl_easy_setopt(easy, option, value) != CURLE_OK)            \
		return (false);                                             \
} while (0)
	SETOPT(CURLOPT_URL, options->url);
	SETOPT(CURLOPT_TLMSP_CFG_FILE, options->config);
	SETOPT(CURLOPT_SSL_VERIFYPEER, 0L);
	SETOPT(CURLOPT_SSL_VERIFYHOST, 0L);
	SETOPT(CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_1_1);
	SETOPT(CURLOPT_HTTPHEADER, headers);
	SETOPT(CURLOPT_POSTFIELDS, "{\"operation\":\"init\"}");
	SETOPT(CURLOPT_POSTFIELDSIZE, 20L);
	SETOPT(CURLOPT_USERAGENT, "tlmsp-benchmark/1");
	SETOPT(CURLOPT_WRITEFUNCTION, capture_body);
	SETOPT(CURLOPT_WRITEDATA, response);
	SETOPT(CURLOPT_NOSIGNAL, 1L);
	SETOPT(CURLOPT_TCP_NODELAY, 1L);
	SETOPT(CURLOPT_TIMEOUT_MS, 10000L);
#undef SETOPT
	return (true);
}

static void
emit_window(const char *event)
{
	fprintf(stderr, "TLMSP_WINDOW event=%s ts_ns=%" PRIu64 "\n",
	    event, wallclock_ns());
	fflush(stderr);
}

static bool
emit_result(CURL *easy, const struct response_buffer *response,
    uint64_t transfer, uint64_t sample, uint64_t scheduled, bool warmup,
    CURLcode code)
{
	long http_code = 0;
	long num_connects = 0;
	long local_port = 0;
	bool body_ok;
	bool success;

	curl_easy_getinfo(easy, CURLINFO_RESPONSE_CODE, &http_code);
	curl_easy_getinfo(easy, CURLINFO_NUM_CONNECTS, &num_connects);
	curl_easy_getinfo(easy, CURLINFO_LOCAL_PORT, &local_port);
	body_ok = response_is_expected(response);
	success = code == CURLE_OK && http_code == 200 && body_ok;
	fprintf(stderr,
	    "TLMSP_RESULT component=driver transfer=%" PRIu64
	    " sample=%" PRIu64 " scheduled=%" PRIu64 " warmup=%u"
	    " curl_code=%d http_code=%ld num_connects=%ld local_port=%ld"
	    " body_ok=%u\n",
	    transfer, sample, scheduled, warmup ? 1U : 0U, (int)code,
	    http_code, num_connects, local_port, body_ok ? 1U : 0U);
	fflush(stderr);
	return (success);
}

static bool
perform_easy(CURL *easy, struct response_buffer *response,
    struct counters *counters, uint64_t sample, uint64_t scheduled,
    bool warmup)
{
	CURLcode code;
	bool success;

	reset_response(response);
	counters->transfer++;
	code = curl_easy_perform(easy);
	success = emit_result(easy, response, counters->transfer, sample,
	    scheduled, warmup, code);
	if (!warmup) {
		counters->launched++;
		counters->completed++;
		if (success)
			counters->successes++;
		else
			counters->errors++;
	}
	return (success);
}

static uint64_t
interval_ns(const struct options *options)
{
	if (options->duration_mode)
		return ((uint64_t)((double)NS_PER_SECOND / options->rate + 0.5));
	return ((uint64_t)options->interval_ms * UINT64_C(1000000));
}

static int
run_persistent(const struct options *options, struct curl_slist *headers,
    struct counters *counters)
{
	struct response_buffer response = {0};
	CURL *easy;
	uint64_t start_ns;
	uint64_t done_ns;
	uint64_t step_ns;
	uint64_t slot = 0;
	uint64_t end_ns = 0;
	unsigned int i;
	int result = 0;

	easy = curl_easy_init();
	if (easy == NULL ||
	    !set_common_options(easy, options, headers, &response)) {
		result = 1;
		goto out;
	}
	for (i = 0; i < options->warmup; i++)
		(void)perform_easy(easy, &response, counters, 0, i + 1, true);
	if (options->warmup_pause_ms > 0)
		(void)sleep_until_ns(wallclock_ns() +
		    (uint64_t)options->warmup_pause_ms * UINT64_C(1000000));

	step_ns = interval_ns(options);
	start_ns = wallclock_ns();
	if (options->duration_mode)
		end_ns = start_ns + (uint64_t)(options->duration_seconds *
		    (double)NS_PER_SECOND);
	emit_window("start");

	while ((!options->duration_mode && slot < options->requests) ||
	    (options->duration_mode && start_ns + slot * step_ns < end_ns)) {
		uint64_t deadline = start_ns + slot * step_ns;
		uint64_t now;

		if (sleep_until_ns(deadline) != 0) {
			result = 1;
			break;
		}
		counters->scheduled++;
		(void)perform_easy(easy, &response, counters, slot + 1,
		    slot + 1, false);
		slot++;

		if (!options->duration_mode)
			continue;
		now = wallclock_ns();
		while (start_ns + slot * step_ns < end_ns &&
		    start_ns + slot * step_ns < now) {
			counters->scheduled++;
			counters->missed++;
			slot++;
		}
	}
	if (options->duration_mode && wallclock_ns() < end_ns)
		(void)sleep_until_ns(end_ns);
	done_ns = wallclock_ns();
	emit_window("done");
	fprintf(stderr,
	    "TLMSP_SUMMARY mode=persistent scheduled=%" PRIu64
	    " launched=%" PRIu64 " completed=%" PRIu64
	    " successes=%" PRIu64 " errors=%" PRIu64 " missed=%" PRIu64
	    " elapsed_ms=%.6f achieved_rps=%.6f max_in_flight=%u\n",
	    counters->scheduled, counters->launched, counters->completed,
	    counters->successes, counters->errors, counters->missed,
	    (done_ns - start_ns) / 1000000.0,
	    done_ns > start_ns ? counters->successes * (double)NS_PER_SECOND /
	    (done_ns - start_ns) : 0.0, 1U);
	fflush(stderr);

out:
	if (easy != NULL)
		curl_easy_cleanup(easy);
	free(response.data);
	return (result);
}

static struct fresh_transfer *
new_fresh_transfer(const struct options *options, struct curl_slist *headers,
    struct counters *counters, uint64_t sample, uint64_t scheduled)
{
	struct fresh_transfer *transfer;

	transfer = calloc(1, sizeof(*transfer));
	if (transfer == NULL)
		return (NULL);
	transfer->easy = curl_easy_init();
	if (transfer->easy == NULL ||
	    !set_common_options(transfer->easy, options, headers,
	    &transfer->response) ||
	    curl_easy_setopt(transfer->easy, CURLOPT_FRESH_CONNECT, 1L) != CURLE_OK ||
	    curl_easy_setopt(transfer->easy, CURLOPT_FORBID_REUSE, 1L) != CURLE_OK ||
	    curl_easy_setopt(transfer->easy, CURLOPT_PRIVATE, transfer) != CURLE_OK) {
		if (transfer->easy != NULL)
			curl_easy_cleanup(transfer->easy);
		free(transfer->response.data);
		free(transfer);
		return (NULL);
	}
	transfer->transfer = ++counters->transfer;
	transfer->sample = sample;
	transfer->scheduled = scheduled;
	return (transfer);
}

static void
complete_fresh(CURLM *multi, CURLMsg *message, struct counters *counters)
{
	struct fresh_transfer *transfer = NULL;
	char *private_data = NULL;
	bool success;

	curl_easy_getinfo(message->easy_handle, CURLINFO_PRIVATE, &private_data);
	transfer = (struct fresh_transfer *)private_data;
	if (transfer == NULL)
		return;
	success = emit_result(transfer->easy, &transfer->response,
	    transfer->transfer, transfer->sample, transfer->scheduled, false,
	    message->data.result);
	counters->completed++;
	if (success)
		counters->successes++;
	else
		counters->errors++;
	if (counters->active > 0)
		counters->active--;
	curl_multi_remove_handle(multi, transfer->easy);
	curl_easy_cleanup(transfer->easy);
	free(transfer->response.data);
	free(transfer);
}

static int
run_fresh(const struct options *options, struct curl_slist *headers,
    struct counters *counters)
{
	CURLM *multi = NULL;
	uint64_t start_ns;
	uint64_t done_ns;
	uint64_t end_ns = 0;
	uint64_t step_ns;
	uint64_t slot = 0;
	unsigned int i;
	int running = 0;
	int result = 0;
	bool scheduling = true;

	for (i = 0; i < options->warmup; i++) {
		struct response_buffer response = {0};
		CURL *easy = curl_easy_init();
		if (easy == NULL || !set_common_options(easy, options, headers,
		    &response)) {
			if (easy != NULL)
				curl_easy_cleanup(easy);
			free(response.data);
			return (1);
		}
		curl_easy_setopt(easy, CURLOPT_FRESH_CONNECT, 1L);
		curl_easy_setopt(easy, CURLOPT_FORBID_REUSE, 1L);
		(void)perform_easy(easy, &response, counters, 0, i + 1, true);
		curl_easy_cleanup(easy);
		free(response.data);
	}
	if (options->warmup_pause_ms > 0)
		(void)sleep_until_ns(wallclock_ns() +
		    (uint64_t)options->warmup_pause_ms * UINT64_C(1000000));

	multi = curl_multi_init();
	if (multi == NULL)
		return (1);
	step_ns = interval_ns(options);
	start_ns = wallclock_ns();
	if (options->duration_mode)
		end_ns = start_ns + (uint64_t)(options->duration_seconds *
		    (double)NS_PER_SECOND);
	emit_window("start");

	while (scheduling || counters->active > 0) {
		uint64_t now = wallclock_ns();
		uint64_t deadline = start_ns + slot * step_ns;

		if (scheduling && now >= deadline) {
			if (options->duration_mode) {
				uint64_t latest = (now - start_ns) / step_ns;
				while (slot < latest &&
				    start_ns + slot * step_ns < end_ns) {
					counters->scheduled++;
					counters->missed++;
					slot++;
				}
			}
			if ((!options->duration_mode && slot >= options->requests) ||
			    (options->duration_mode &&
			    start_ns + slot * step_ns >= end_ns)) {
				scheduling = false;
			} else {
				struct fresh_transfer *transfer;
				counters->scheduled++;
				if (counters->active >= options->max_in_flight) {
					counters->missed++;
				} else {
					transfer = new_fresh_transfer(options, headers,
					    counters, slot + 1, slot + 1);
					if (transfer == NULL ||
					    curl_multi_add_handle(multi, transfer->easy) != CURLM_OK) {
						result = 1;
						if (transfer != NULL) {
							curl_easy_cleanup(transfer->easy);
							free(transfer->response.data);
							free(transfer);
						}
						break;
					}
					counters->launched++;
					counters->active++;
					if (counters->active > counters->max_active)
						counters->max_active = counters->active;
				}
				slot++;
			}
		}

		if (curl_multi_perform(multi, &running) != CURLM_OK) {
			result = 1;
			break;
		}
		for (;;) {
			CURLMsg *message;
			int remaining;
			message = curl_multi_info_read(multi, &remaining);
			if (message == NULL)
				break;
			if (message->msg == CURLMSG_DONE)
				complete_fresh(multi, message, counters);
		}

		if (scheduling &&
		    ((!options->duration_mode && slot >= options->requests) ||
		    (options->duration_mode && start_ns + slot * step_ns >= end_ns)))
			scheduling = false;
		if (!scheduling && counters->active == 0)
			break;

		if (scheduling) {
			uint64_t next_ns = start_ns + slot * step_ns;
			now = wallclock_ns();
			if (counters->active == 0 && next_ns > now) {
				if (sleep_until_ns(next_ns) != 0) {
					result = 1;
					break;
				}
			} else {
				uint64_t wait_ns = next_ns > now ? next_ns - now : 0;
				int wait_ms = (int)(wait_ns / UINT64_C(1000000));
				int numfds;
				if (wait_ms > 100)
					wait_ms = 100;
				if (curl_multi_wait(multi, NULL, 0, wait_ms, &numfds) != CURLM_OK) {
					result = 1;
					break;
				}
			}
		} else if (counters->active > 0) {
			int numfds;
			if (curl_multi_wait(multi, NULL, 0, 100, &numfds) != CURLM_OK) {
				result = 1;
				break;
			}
		}
	}

	if (options->duration_mode && wallclock_ns() < end_ns)
		(void)sleep_until_ns(end_ns);
	done_ns = wallclock_ns();
	emit_window("done");
	fprintf(stderr,
	    "TLMSP_SUMMARY mode=fresh scheduled=%" PRIu64
	    " launched=%" PRIu64 " completed=%" PRIu64
	    " successes=%" PRIu64 " errors=%" PRIu64 " missed=%" PRIu64
	    " elapsed_ms=%.6f achieved_rps=%.6f max_in_flight=%u\n",
	    counters->scheduled, counters->launched, counters->completed,
	    counters->successes, counters->errors, counters->missed,
	    (done_ns - start_ns) / 1000000.0,
	    done_ns > start_ns ? counters->successes * (double)NS_PER_SECOND /
	    (done_ns - start_ns) : 0.0, counters->max_active);
	fflush(stderr);
	curl_multi_cleanup(multi);
	return (result);
}

int
main(int argc, char **argv)
{
	struct options options = {
		.mode = MODE_PERSISTENT,
		.url = DEFAULT_URL,
		.config = DEFAULT_CONFIG,
		.warmup = 1,
		.requests = 1000,
		.interval_ms = 1000,
		.warmup_pause_ms = 0,
		.max_in_flight = 1024,
	};
	struct counters counters = {0};
	struct curl_slist *headers = NULL;
	int option;
	int option_index;
	int result;
	static const struct option long_options[] = {
		{"mode", required_argument, NULL, 'm'},
		{"url", required_argument, NULL, 'u'},
		{"config", required_argument, NULL, 'c'},
		{"warmup", required_argument, NULL, 'w'},
		{"requests", required_argument, NULL, 'n'},
		{"interval-ms", required_argument, NULL, 'i'},
		{"duration-seconds", required_argument, NULL, 'd'},
		{"rate", required_argument, NULL, 'r'},
		{"warmup-pause-ms", required_argument, NULL, 'p'},
		{"max-in-flight", required_argument, NULL, 'f'},
		{"help", no_argument, NULL, 'h'},
		{NULL, 0, NULL, 0}
	};

	while ((option = getopt_long(argc, argv, "m:u:c:w:n:i:d:r:p:f:h",
	    long_options, &option_index)) != -1) {
		switch (option) {
		case 'm':
			if (strcmp(optarg, "persistent") == 0)
				options.mode = MODE_PERSISTENT;
			else if (strcmp(optarg, "fresh") == 0)
				options.mode = MODE_FRESH;
			else
				goto bad_usage;
			break;
		case 'u': options.url = optarg; break;
		case 'c': options.config = optarg; break;
		case 'w':
			if (!parse_nonnegative(optarg, &options.warmup))
				goto bad_usage;
			break;
		case 'n':
			if (!parse_nonnegative(optarg, &options.requests))
				goto bad_usage;
			break;
		case 'i':
			if (!parse_nonnegative(optarg, &options.interval_ms))
				goto bad_usage;
			break;
		case 'd':
			if (!parse_positive_double(optarg, &options.duration_seconds))
				goto bad_usage;
			options.duration_mode = true;
			break;
		case 'r':
			if (!parse_positive_double(optarg, &options.rate))
				goto bad_usage;
			break;
		case 'p':
			if (!parse_nonnegative(optarg, &options.warmup_pause_ms))
				goto bad_usage;
			break;
		case 'f':
			if (!parse_nonnegative(optarg, &options.max_in_flight) ||
			    options.max_in_flight == 0)
				goto bad_usage;
			break;
		case 'h': usage(argv[0]); return (0);
		default: goto bad_usage;
		}
	}
	if ((!options.duration_mode && options.requests == 0) ||
	    (options.duration_mode &&
	    (options.rate <= 0.0 || interval_ns(&options) == 0)))
		goto bad_usage;

	if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK)
		return (1);
	headers = curl_slist_append(headers, "X-Testing: 1");
	headers = curl_slist_append(headers, "Authorization: Bearer token");
	headers = curl_slist_append(headers, "Content-Type: application/json");
	if (headers == NULL) {
		curl_global_cleanup();
		return (1);
	}
	if (options.mode == MODE_PERSISTENT)
		result = run_persistent(&options, headers, &counters);
	else
		result = run_fresh(&options, headers, &counters);

	curl_slist_free_all(headers);
	curl_global_cleanup();
	if (result != 0)
		return (result);
	return (counters.errors == 0 ? 0 : 2);

bad_usage:
	usage(argv[0]);
	return (64);
}
