// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once
#include <cstddef>
#define OBS_DECLARE_MODULE()
#define OBS_MODULE_AUTHOR(name)
#define MODULE_EXPORT extern "C"
enum obs_frontend_event { OBS_FRONTEND_EVENT_EXIT, OBS_FRONTEND_EVENT_FINISHED_LOADING,
    OBS_FRONTEND_EVENT_STREAMING_STARTING, OBS_FRONTEND_EVENT_STREAMING_STARTED,
    OBS_FRONTEND_EVENT_STREAMING_STOPPING, OBS_FRONTEND_EVENT_STREAMING_STOPPED };
using obs_frontend_cb = void (*)(void *);
using obs_frontend_event_cb = void (*)(obs_frontend_event, void *);
struct obs_service_t { bool unused; };
struct obs_data_t { bool unused; };
struct obs_output_t { bool unused; };
inline constexpr int LOG_INFO = 300;
inline constexpr int LOG_WARNING = 200;
void blog(int level, const char *format, ...);
const char *obs_get_version_string();
void obs_frontend_add_tools_menu_item(const char *, obs_frontend_cb, void *);
void obs_frontend_add_event_callback(obs_frontend_event_cb, void *);
void obs_frontend_remove_event_callback(obs_frontend_event_cb, void *);
bool obs_frontend_streaming_active();
bool obs_frontend_recording_active();
void obs_frontend_streaming_stop();
char *obs_frontend_get_current_profile();
obs_service_t *obs_frontend_get_streaming_service();
obs_output_t *obs_frontend_get_streaming_output();
bool obs_output_active(const obs_output_t *);
bool obs_output_reconnecting(const obs_output_t *);
obs_service_t *obs_output_get_service(const obs_output_t *);
void obs_output_release(obs_output_t *);
void obs_output_stop(obs_output_t *);
obs_data_t *obs_service_get_settings(const obs_service_t *);
const char *obs_data_get_string(const obs_data_t *, const char *);
void obs_data_release(obs_data_t *);
void bfree(void *);
