/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* Wi-Fi CSI console Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <errno.h>
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_err.h"
#include "esp_console.h"

#include "esp_mac.h"
#include "esp_wifi.h"
#include "lwip/inet.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "ping/ping_sock.h"
#include "hal/uart_ll.h"
#include "mbedtls/base64.h"

#include "led_strip.h"
#include "esp_radar.h"
#include "csi_commands.h"

extern esp_ping_handle_t g_ping_handle;
static led_strip_handle_t led_strip;
#if CONFIG_IDF_TARGET_ESP32C5
#define WS2812_GPIO 27
#elif CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define WS2812_GPIO 8
#elif CONFIG_IDF_TARGET_ESP32S3
#define WS2812_GPIO 48  /* ESP32-S3-DevKitC-1's onboard WS2812 is on GPIO48, not 38 */
#elif CONFIG_IDF_TARGET_ESP32S2
#define WS2812_GPIO 18
#elif CONFIG_IDF_TARGET_ESP32C3
#define WS2812_GPIO 8
#else
#define WS2812_GPIO 4
#endif
#define RECV_ESPNOW_CSI
#define CONFIG_LESS_INTERFERENCE_CHANNEL    11
#define CONFIG_SEND_DATA_FREQUENCY          100

#define RADAR_EVALUATE_SERVER_PORT          3232
#define RADAR_BUFF_MAX_LEN                  25

static QueueHandle_t g_csi_info_queue    = NULL;
static bool g_wifi_connect_status        = false;
static uint32_t g_send_data_interval     = 1000 / CONFIG_SEND_DATA_FREQUENCY;
static const char *TAG                   = "app_main";

static struct {
    struct arg_lit *train_start;
    struct arg_lit *train_stop;
    struct arg_lit *train_add;
    struct arg_str *predict_someone_threshold;
    struct arg_str *predict_someone_sensitivity;
    struct arg_str *predict_move_threshold;
    struct arg_str *predict_move_sensitivity;
    struct arg_int *predict_buff_size;
    struct arg_int *predict_outliers_number;
    struct arg_str *collect_taget;
    struct arg_int *collect_number;
    struct arg_int *collect_duration;
    struct arg_lit *csi_start;
    struct arg_lit *csi_stop;
    struct arg_str *csi_output_type;
    struct arg_str *csi_output_format;
    struct arg_int *csi_scale_shift;
    struct arg_int *channel_filter;
    struct arg_int *send_data_interval;
    struct arg_end *end;
} radar_args;
static struct console_input_config {
    bool train_start;
    float predict_someone_threshold;
    float predict_someone_sensitivity;
    float predict_move_threshold;
    float predict_move_sensitivity;
    uint32_t predict_buff_size;
    uint32_t predict_outliers_number;
    char collect_taget[16];
    uint32_t collect_number;
    char csi_output_type[16];
    char csi_output_format[16];
} g_console_input_config = {
    .predict_someone_threshold = 0,
    .predict_someone_sensitivity = 0.15,
    .predict_move_threshold    = 0.0003,
    .predict_move_sensitivity  = 0.20,
    .predict_buff_size         = 5,
    .predict_outliers_number   = 2,
    .train_start               = false,
    .collect_taget             = "unknown",
    .csi_output_type           = "LLTF",
    .csi_output_format         = "decimal"
};

static TimerHandle_t g_collect_timer_handele = NULL;

void wifi_csi_raw_cb(void *ctx, const wifi_csi_filtered_info_t *info)
{
    wifi_csi_filtered_info_t *q_data = malloc(sizeof(wifi_csi_filtered_info_t) + info->valid_len);
    *q_data = *info;
    memcpy(q_data->valid_data, info->valid_data, info->valid_len);

    if (!g_csi_info_queue || xQueueSend(g_csi_info_queue, &q_data, 0) == pdFALSE) {
        ESP_LOGW(TAG, "g_csi_info_queue full");
        free(q_data);
    }
}

static void collect_timercb(TimerHandle_t timer)
{
    g_console_input_config.collect_number--;

    if (!g_console_input_config.collect_number) {
        xTimerStop(g_collect_timer_handele, 0);
        xTimerDelete(g_collect_timer_handele, 0);
        g_collect_timer_handele = NULL;
        strcpy(g_console_input_config.collect_taget, "unknown");
        return;
    }
}

static int wifi_cmd_radar(int argc, char **argv)
{
    if (arg_parse(argc, argv, (void **) &radar_args) != ESP_OK) {
        arg_print_errors(stderr, radar_args.end, argv[0]);
        return ESP_FAIL;
    }

    if (radar_args.train_start->count) {
        if (!radar_args.train_add->count) {
            esp_radar_train_remove();
        }

        esp_radar_train_start();
        g_console_input_config.train_start = true;
    }

    if (radar_args.train_stop->count) {
        esp_radar_train_stop(&g_console_input_config.predict_someone_threshold,
                             &g_console_input_config.predict_move_threshold);
        g_console_input_config.train_start = false;

        printf("RADAR_DADA,0,0,0,%.6f,0,0,%.6f,0\n",
               g_console_input_config.predict_someone_threshold,
               g_console_input_config.predict_move_threshold);
    }

    if (radar_args.predict_move_threshold->count) {
        g_console_input_config.predict_move_threshold = atof(radar_args.predict_move_threshold->sval[0]);
    }

    if (radar_args.predict_move_sensitivity->count) {
        g_console_input_config.predict_move_sensitivity = atof(radar_args.predict_move_sensitivity->sval[0]);
        ESP_LOGI(TAG, "predict_move_sensitivity: %f", g_console_input_config.predict_move_sensitivity);
    }

    if (radar_args.predict_someone_threshold->count) {
        g_console_input_config.predict_someone_threshold = atof(radar_args.predict_someone_threshold->sval[0]);
    }

    if (radar_args.predict_someone_sensitivity->count) {
        g_console_input_config.predict_someone_sensitivity = atof(radar_args.predict_someone_sensitivity->sval[0]);
        ESP_LOGI(TAG, "predict_someone_sensitivity: %f", g_console_input_config.predict_someone_sensitivity);
    }

    if (radar_args.predict_buff_size->count) {
        g_console_input_config.predict_buff_size = radar_args.predict_buff_size->ival[0];
    }

    if (radar_args.predict_outliers_number->count) {
        g_console_input_config.predict_outliers_number = radar_args.predict_outliers_number->ival[0];
    }

    if (radar_args.collect_taget->count && radar_args.collect_number->count && radar_args.collect_duration->count) {
        g_console_input_config.collect_number = radar_args.collect_number->ival[0];
        strcpy(g_console_input_config.collect_taget, radar_args.collect_taget->sval[0]);

        if (g_collect_timer_handele) {
            xTimerStop(g_collect_timer_handele, portMAX_DELAY);
            xTimerDelete(g_collect_timer_handele, portMAX_DELAY);
        }

        g_collect_timer_handele = xTimerCreate("collect", pdMS_TO_TICKS(radar_args.collect_duration->ival[0]),
                                               true, NULL, collect_timercb);
        xTimerStart(g_collect_timer_handele, portMAX_DELAY);
    }

    if (radar_args.csi_output_format->count) {
        strcpy(g_console_input_config.csi_output_format, radar_args.csi_output_format->sval[0]);
    }

    if (radar_args.csi_output_type->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);

        if (!strcasecmp(radar_args.csi_output_type->sval[0], "NULL")) {
            radar_config.csi_config.csi_filtered_cb = NULL;
        } else {
            radar_config.csi_config.csi_filtered_cb = wifi_csi_raw_cb;
            strcpy(g_console_input_config.csi_output_type, radar_args.csi_output_type->sval[0]);
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
            if (!strcasecmp(radar_args.csi_output_type->sval[0], "LLTF")) {
                radar_config.csi_config.acquire_csi_lltf = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_LLTF;
                radar_config.dec_config.sub_carrier_step_size = 2;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "HT-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_ht20 = true;
                radar_config.csi_config.acquire_csi_ht40 = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_HTLTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "STBC-HT-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_ht20 = true;
                radar_config.csi_config.acquire_csi_ht40 = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_STBC_HTLTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "HE-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_su = true;
                radar_config.csi_config.acquire_csi_mu = true;
                radar_config.csi_config.acquire_csi_dcm = true;
                radar_config.csi_config.acquire_csi_beamformed = true;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_HELTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            } else if (!strcasecmp(radar_args.csi_output_type->sval[0], "STBC-HE-LTF")) {
                radar_config.csi_config.acquire_csi_lltf = false;
                radar_config.csi_config.acquire_csi_su = true;
                radar_config.csi_config.acquire_csi_mu = true;
                radar_config.csi_config.acquire_csi_dcm = true;
                radar_config.csi_config.acquire_csi_beamformed = true;
                radar_config.csi_config.acquire_csi_he_stbc_mode = CSI_HE_STBC_MODE_AVERAGE;
                radar_config.csi_config.acquire_csi_vht = true;
                radar_config.dec_config.ltf_type = RADAR_LTF_TYPE_STBC_HELTF;
                radar_config.dec_config.sub_carrier_step_size = 5;
            }
#endif
        }

        esp_radar_change_config(&radar_config);
    }

    if (radar_args.csi_start->count) {
        esp_radar_start();
    }

    if (radar_args.csi_stop->count) {
        esp_radar_stop();
    }

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32
    if (radar_args.csi_scale_shift->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.shift = radar_args.csi_scale_shift->ival[0];
        esp_radar_change_config(&radar_config);

        ESP_LOGI(TAG, "manually left shift %d bits of the scale of the CSI data", radar_config.csi_config.shift);
    }

    if (radar_args.channel_filter->count) {
        esp_radar_config_t radar_config = {0};
        esp_radar_get_config(&radar_config);
        radar_config.csi_config.channel_filter_en = radar_args.channel_filter->ival[0];
        esp_radar_change_config(&radar_config);

        ESP_LOGI(TAG, "enable(%d) to turn on channel filter to smooth adjacent sub-carrier", radar_config.csi_config.channel_filter_en);
    }
#endif
    if (radar_args.send_data_interval->count) {
        g_send_data_interval = radar_args.send_data_interval->ival[0];
    }

    return ESP_OK;
}

void cmd_register_radar(void)
{
    radar_args.train_start = arg_lit0(NULL, "train_start", "Start calibrating the 'Radar' algorithm");
    radar_args.train_stop  = arg_lit0(NULL, "train_stop", "Stop calibrating the 'Radar' algorithm");
    radar_args.train_add   = arg_lit0(NULL, "train_add", "Calibrate on the basis of saving the calibration results");

    radar_args.predict_someone_threshold = arg_str0(NULL, "predict_someone_threshold", "<0 ~ 1.0>", "Configure the threshold for someone");
    radar_args.predict_someone_sensitivity  = arg_str0(NULL, "predict_someone_sensitivity", "<0 ~ 1.0>", "Configure the sensitivity for someone");
    radar_args.predict_move_threshold    = arg_str0(NULL, "predict_move_threshold", "<0 ~ 1.0>", "Configure the threshold for move");
    radar_args.predict_move_sensitivity  = arg_str0(NULL, "predict_move_sensitivity", "<0 ~ 1.0>", "Configure the sensitivity for move");
    radar_args.predict_buff_size         = arg_int0(NULL, "predict_buff_size", "1 ~ 100", "Buffer size for filtering outliers");
    radar_args.predict_outliers_number   = arg_int0(NULL, "predict_outliers_number", "<1 ~ 100>", "The number of items in the buffer queue greater than the threshold");

    radar_args.collect_taget    = arg_str0(NULL, "collect_tagets", "<0 ~ 20>", "Type of CSI data collected");
    radar_args.collect_number   = arg_int0(NULL, "collect_number", "sequence", "Number of times CSI data was collected");
    radar_args.collect_duration = arg_int0(NULL, "collect_duration", "duration", "Time taken to acquire one CSI data");

    radar_args.csi_start         = arg_lit0(NULL, "csi_start", "Start collecting CSI data from Wi-Fi");
    radar_args.csi_stop          = arg_lit0(NULL, "csi_stop", "Stop CSI data collection from Wi-Fi");
    radar_args.csi_output_type   = arg_str0(NULL, "csi_output_type", "<NULL, LLTF, HT-LTF, HE-LTF, STBC-HT-LTF, STBC-HE-LTF>", "Type of CSI data");
    radar_args.csi_output_format = arg_str0(NULL, "csi_output_format", "<decimal, base64>", "Format of CSI data");
    radar_args.csi_scale_shift   = arg_int0(NULL, "scale_shift", "<0~15>", "manually left shift bits of the scale of the CSI data");
    radar_args.channel_filter    = arg_int0(NULL, "channel_filter", "<0 or 1>", "enable to turn on channel filter to smooth adjacent sub-carrier");

    radar_args.send_data_interval = arg_int0(NULL, "send_data_interval", "<interval_ms>", "The interval between sending null data or ping packets to the router");

    radar_args.end                = arg_end(8);

    const esp_console_cmd_t radar_cmd = {
        .command = "radar",
        .help = "Radar config",
        .hint = NULL,
        .func = &wifi_cmd_radar,
        .argtable = &radar_args
    };

    ESP_ERROR_CHECK(esp_console_cmd_register(&radar_cmd));
}

#define SPECTRUM_LOCAL_PORT 9998

static int g_udp_sock = -1;
static struct sockaddr_in g_server_addr;

static void udp_sender_init(void)
{
    g_udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (g_udp_sock < 0) {
        ESP_LOGE(TAG, "failed to create UDP socket: errno %d", errno);
        return;
    }

    /* Bound to a fixed local port so the Pi can send commands (LED on/off)
     * back to us: it just replies to whatever addr our packets arrive
     * from, which only stays stable across packets if we don't leave the
     * port to be picked at random by the first sendto(). */
    struct sockaddr_in local_addr = {
        .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port = htons(SPECTRUM_LOCAL_PORT),
    };
    if (bind(g_udp_sock, (struct sockaddr *)&local_addr, sizeof(local_addr)) < 0) {
        ESP_LOGW(TAG, "bind failed: errno %d (commands from the Pi won't reach us)", errno);
    } else {
        ESP_LOGI(TAG, "bound to local port %d for incoming commands", SPECTRUM_LOCAL_PORT);
    }

    g_server_addr.sin_family = AF_INET;
    g_server_addr.sin_port = htons(CONFIG_SPECTRUM_SERVER_PORT);
    g_server_addr.sin_addr.s_addr = inet_addr(CONFIG_SPECTRUM_SERVER_HOST);

    ESP_LOGI(TAG, "sending raw CSI to %s:%d as node_id=%d (listening on :%d for commands)",
             CONFIG_SPECTRUM_SERVER_HOST, CONFIG_SPECTRUM_SERVER_PORT, CONFIG_SPECTRUM_NODE_ID, SPECTRUM_LOCAL_PORT);
}

/* Non-blocking: picks up a {"led": true/false} command from the Pi if one
 * arrived, driving the big WS2812 LED directly off the Pi's presence
 * decision. Deliberately dumb parsing (substring search) rather than
 * pulling in a JSON parser for one boolean. */
static void poll_led_command(void)
{
    char buf[32];
    int n = recv(g_udp_sock, buf, sizeof(buf) - 1, MSG_DONTWAIT);
    if (n <= 0) {
        if (n < 0 && errno != EWOULDBLOCK && errno != EAGAIN) {
            ESP_LOGW(TAG, "recv failed: errno %d", errno);
        }
        return;
    }
    buf[n] = '\0';
    ESP_LOGI(TAG, "led command received: %s", buf);

    if (strstr(buf, "true")) {
        led_strip_set_pixel(led_strip, 0, 3, 0, 4);
        led_strip_refresh(led_strip);
    } else if (strstr(buf, "false")) {
        led_strip_clear(led_strip);
    }
}

/* Sends each captured CSI frame straight to the Pi as a raw JSON UDP
 * packet -- no on-device aggregation or scoring, that's computed
 * server-side so thresholds can be tuned without reflashing. */
static void csi_data_print_task(void *arg)
{
    wifi_csi_filtered_info_t *info = NULL;
    char *buffer = malloc(8 * 1024);

    udp_sender_init();

    while (xQueueReceive(g_csi_info_queue, &info, portMAX_DELAY)) {
        size_t len = 0;
        esp_radar_rx_ctrl_info_t *rx_ctrl = &info->rx_ctrl_info;

        uint16_t valid_len = info->valid_len;
        if (!strcasecmp(g_console_input_config.csi_output_type, "LLTF")) {
            info->valid_len = info->valid_lltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "HT-LTF")) {
            info->valid_len = info->valid_lltf_len + info->valid_ht_ltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "STBC-HT-LTF")) {
            info->valid_len = info->valid_lltf_len + info->valid_ht_ltf_len + info->valid_stbc_ht_ltf_len;
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "HE-LTF")) {
            info->valid_len = info->valid_he_ltf_len;
        } else if (!strcasecmp(g_console_input_config.csi_output_type, "STBC-HE-LTF")) {
            info->valid_len = info->valid_he_ltf_len + info->valid_stbc_he_ltf_len;
#endif
        }
        if (info->valid_len == 0) {
            info->valid_len = valid_len;
        }

        len += sprintf(buffer + len, "{\"node_id\":%d,\"timestamp\":%u,\"rssi\":%d,\"csi\":[",
                       CONFIG_SPECTRUM_NODE_ID, esp_log_timestamp(), rx_ctrl->rssi);

        for (int i = 0; i < info->valid_len; i++) {
            len += sprintf(buffer + len, i == 0 ? "%d" : ",%d", info->valid_data[i]);
        }

        len += sprintf(buffer + len, "]}");

        if (g_udp_sock >= 0) {
            int ret = sendto(g_udp_sock, buffer, len, 0, (struct sockaddr *)&g_server_addr, sizeof(g_server_addr));
            if (ret < 0) {
                ESP_LOGW(TAG, "sendto failed: errno %d", errno);
            }
            poll_led_command();
        }

        free(info);
    }

    free(buffer);
    vTaskDelete(NULL);
}

static void trigger_router_send_data_task(void *arg)
{
    esp_radar_config_t radar_config = {0};
    wifi_ap_record_t ap_info         = {0};
    uint8_t sta_mac[6]               = {0};

    esp_radar_get_config(&radar_config);
    esp_wifi_sta_get_ap_info(&ap_info);
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, sta_mac));

    radar_config.csi_config.csi_recv_interval = g_send_data_interval;
    memcpy(radar_config.csi_config.filter_dmac, sta_mac, sizeof(radar_config.csi_config.filter_dmac));

#if WIFI_CSI_SEND_NULL_DATA_ENABLE
    ESP_LOGI(TAG, "Send null data to router");

    memset(radar_config.csi_config.filter_mac, 0, sizeof(radar_config.csi_config.filter_mac));
    esp_radar_change_config(&radar_config);

    typedef struct {
        uint8_t frame_control[2];
        uint16_t duration;
        uint8_t destination_address[6];
        uint8_t source_address[6];
        uint8_t broadcast_address[6];
        uint16_t sequence_control;
    } __attribute__((packed)) wifi_null_data_t;

    wifi_null_data_t null_data = {
        .frame_control       = {0x48, 0x01},
        .duration            = 0x0000,
        .sequence_control    = 0x0000,
    };

    memcpy(null_data.destination_address, ap_info.bssid, 6);
    memcpy(null_data.broadcast_address, ap_info.bssid, 6);
    memcpy(null_data.source_address, sta_mac, 6);

    ESP_LOGW(TAG, "null_data, destination_address: "MACSTR", source_address: "MACSTR", broadcast_address: " MACSTR,
             MAC2STR(null_data.destination_address), MAC2STR(null_data.source_address), MAC2STR(null_data.broadcast_address));

    ESP_ERROR_CHECK(esp_wifi_config_80211_tx_rate(WIFI_IF_STA, WIFI_PHY_RATE_6M));

    for (int i = 0; g_wifi_connect_status; i++) {
        esp_err_t ret = esp_wifi_80211_tx(WIFI_IF_STA, &null_data, sizeof(wifi_null_data_t), true);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "esp_wifi_80211_tx, %s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(1000));
        }

        vTaskDelay(pdMS_TO_TICKS(g_send_data_interval));
    }

#else
    ESP_LOGI(TAG, "Send ping data to router");

    memcpy(radar_config.csi_config.filter_mac, ap_info.bssid, sizeof(radar_config.csi_config.filter_mac));
    esp_radar_change_config(&radar_config);

    /* stop and delete existing ping session if any */
    if (g_ping_handle != NULL) {
        ESP_LOGI(TAG, "Stopping existing ping session before starting new one");
        esp_ping_stop(g_ping_handle);
        esp_ping_delete_session(g_ping_handle);
        g_ping_handle = NULL;
        ESP_LOGI(TAG, "Existing ping session stopped and deleted");
    }

    esp_ping_config_t config = ESP_PING_DEFAULT_CONFIG();
    config.count       = 0;
    config.data_size   = 1;
    config.interval_ms = g_send_data_interval;

    /**
     * @brief Get the Router IP information from the esp-netif
     */
    esp_netif_ip_info_t local_ip;
    esp_netif_get_ip_info(esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"), &local_ip);
    ESP_LOGI(TAG, "Ping: got ip:" IPSTR ", gw: " IPSTR, IP2STR(&local_ip.ip), IP2STR(&local_ip.gw));
    config.target_addr.u_addr.ip4.addr = ip4_addr_get_u32(&local_ip.gw);
    config.target_addr.type = ESP_IPADDR_TYPE_V4;

    esp_ping_callbacks_t cbs = { 0 };
    esp_ping_new_session(&config, &cbs, &g_ping_handle);
    esp_ping_start(g_ping_handle);
#endif

    vTaskDelete(NULL);
}

/* Event handler for catching system events */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        g_wifi_connect_status = true;

        xTaskCreate(trigger_router_send_data_task, "trigger_router_send_data", 4 * 1024, NULL, 5, NULL);

#ifdef RECV_ESPNOW_CSI
        ESP_ERROR_CHECK(esp_wifi_set_promiscuous(false));
#endif
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        g_wifi_connect_status = false;
        ESP_LOGW(TAG, "Wi-Fi disconnected");
        esp_radar_config_t radar_config;
        esp_radar_get_config(&radar_config);
        esp_radar_wifi_reinit(&radar_config.wifi_config);

    }
}
esp_err_t ws2812_led_init(void)
{
    led_strip_config_t strip_config = {
        .strip_gpio_num = WS2812_GPIO,
        .max_leds = 1,
    };

#if CONFIG_IDF_TARGET_ESP32C61
    led_strip_spi_config_t spi_config = {
        .clk_src = SPI_CLK_SRC_DEFAULT,
        .spi_bus = SPI2_HOST,
        .flags = {
            .with_dma = false,
        }
    };
    ESP_ERROR_CHECK(led_strip_new_spi_device(&strip_config, &spi_config, &led_strip));
#else
    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .flags = {
            .with_dma = false,
        }
    };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_config, &rmt_config, &led_strip));
#endif
    /* Set all LED off to clear all pixels */
    led_strip_clear(led_strip);
    return ESP_OK;
}
void app_main(void)
{
    ESP_LOGI(TAG, "app_main start, line: %d", __LINE__);
    /**
     * @brief Initialize NVS
     */

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "app_main line: %d", __LINE__);
    /**
     * @brief Install ws2812 driver, Used to display the status of the device
     */
    ws2812_led_init();

    ESP_LOGI(TAG, "app_main start, line: %d", __LINE__);
    /**
     * @brief Turn on the radar module printing information
     */
    esp_log_level_set("esp_radar", ESP_LOG_INFO);

    /**
     * @brief Register serial command
     */
    esp_console_repl_t *repl = NULL;
    esp_console_repl_config_t repl_config = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
    /* This board only exposes the native USB (USB-Serial-JTAG) port, used
     * for both flashing and console; UART0 (GPIO43/44) isn't wired to
     * anything, so the REPL must attach there instead of UART. */
    esp_console_dev_usb_serial_jtag_config_t usb_jtag_config = ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
    repl_config.prompt = "csi>";
    ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb_jtag_config, &repl_config, &repl));

#if CONFIG_IDF_TARGET_ESP32 || CONFIG_IDF_TARGET_ESP32S2
    /**< Fix serial port garbled code due to high baud rate */
    uart_ll_set_sclk(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), UART_SCLK_APB);
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
    uart_ll_set_baudrate(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), CONFIG_ESP_CONSOLE_UART_BAUDRATE, APB_CLK_FREQ);
#else
    uart_ll_set_baudrate(UART_LL_GET_HW(CONFIG_ESP_CONSOLE_UART_NUM), CONFIG_ESP_CONSOLE_UART_BAUDRATE);
#endif
#endif

    /**
     * @brief Set the Wi-Fi radar configuration
     */
    esp_radar_csi_config_t csi_config = ESP_RADAR_CSI_CONFIG_DEFAULT();
    esp_radar_wifi_config_t wifi_config = ESP_RADAR_WIFI_CONFIG_DEFAULT();
    esp_radar_espnow_config_t espnow_config = ESP_RADAR_ESPNOW_CONFIG_DEFAULT();
    esp_radar_dec_config_t dec_config = ESP_RADAR_DEC_CONFIG_DEFAULT();
    memcpy(csi_config.filter_mac, "\x1a\x00\x00\x00\x00\x00", 6);
    csi_config.csi_recv_interval = g_send_data_interval;
    /* Not wifi_radar_cb: that's esp_radar's own on-device room/human scoring,
     * which we don't use (all scoring is server-side, see spectrum/server).
     * It also drove the big LED -- leaving it registered would have it
     * fighting poll_led_command() over the same led_strip. */
    dec_config.wifi_radar_cb     = NULL;
#if WIFI_CSI_SEND_NULL_DATA_ENABLE
    csi_config.dump_ack_en       = true;
#endif
    dec_config.outliers_threshold = 0;
    ESP_ERROR_CHECK(esp_radar_wifi_init(&wifi_config));
    ESP_ERROR_CHECK(esp_radar_csi_init(&csi_config));
    ESP_ERROR_CHECK(esp_radar_dec_init(&dec_config));

    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, &wifi_event_handler, NULL));

    cmd_register_ping();
    cmd_register_system();
    cmd_register_wifi_config();
    cmd_register_wifi_scan();
    cmd_register_radar();
    ESP_ERROR_CHECK(esp_console_start_repl(repl));

    /**
     * @brief Start Wi-Fi radar
     */
    esp_radar_start();

    /**
     * @brief Initialize the UDP-forwarding task that streams raw CSI to the Pi.
     * Uses a task (rather than sending straight from wifi_csi_raw_cb) to
     * avoid blocking the CSI callback.
     */
    g_csi_info_queue = xQueueCreate(64, sizeof(void *));
    xTaskCreate(csi_data_print_task, "csi_data_print", 4 * 1024, NULL, 0, NULL);

    /**
     * @brief Headless boot: auto-join the configured WiFi and enable raw CSI
     * streaming, so this node comes online on its own with no monitor or
     * console interaction -- same commands as typing them at the "csi>"
     * prompt, just run programmatically here.
     */
    if (strlen(CONFIG_SPECTRUM_WIFI_SSID)) {
        int cmd_ret = 0;
        char wifi_connect_cmd[160];
        snprintf(wifi_connect_cmd, sizeof(wifi_connect_cmd), "wifi_config -s %s -p %s",
                 CONFIG_SPECTRUM_WIFI_SSID, CONFIG_SPECTRUM_WIFI_PASSWORD);
        esp_console_run(wifi_connect_cmd, &cmd_ret);

        /* wifi_config returns as soon as the STA gets an IP, racing against
         * trigger_router_send_data_task (spawned off the same GOT_IP event)
         * which sets radar_config.csi_config.filter_mac to the AP's BSSID.
         * Both read-modify-write the same esp_radar config with no lock, so
         * running --csi_output_type immediately can clobber that filter
         * with the default (unset) one. Give it a moment to land first. */
        vTaskDelay(pdMS_TO_TICKS(2000));
        esp_console_run("radar --csi_output_type LLTF", &cmd_ret);
    }
}
