#define STUDY_REJECTION_LIMIT 128

typedef struct {
  JSValue promise;
  JSValue reason;
} StudyRejection;

typedef struct {
  StudyRejection pending[STUDY_REJECTION_LIMIT];
  size_t count;
  bool overflow;
} StudyRejections;

static void study_remove_rejection(JSContext *ctx, StudyRejections *state, size_t index) {
  StudyRejection entry = state->pending[index];
  state->pending[index] = state->pending[--state->count];
  JS_FreeValue(ctx, entry.promise);
  JS_FreeValue(ctx, entry.reason);
}

static void study_track_rejection(JSContext *ctx, JSValueConst promise,
                                  JSValueConst reason, BOOL handled, void *opaque) {
  StudyRejections *state = JS_GetContextOpaque(ctx);
  if (!state) return;
  for (size_t i = 0; i < state->count; i++) {
    if (JS_VALUE_GET_PTR(state->pending[i].promise) == JS_VALUE_GET_PTR(promise)) {
      if (handled) study_remove_rejection(ctx, state, i);
      return;
    }
  }
  if (handled) return;
  if (state->count == STUDY_REJECTION_LIMIT) {
    state->overflow = true;
    return;
  }
  state->pending[state->count++] = (StudyRejection) {
    .promise = JS_DupValue(ctx, promise),
    .reason = JS_DupValue(ctx, reason)
  };
}

static JSValue study_check_rejections(JSContext *ctx, JSValueConst this_val,
                                      int argc, JSValueConst *argv) {
  StudyRejections *state = JS_GetContextOpaque(ctx);
  if (state->overflow) return JS_ThrowRangeError(ctx, "script_rejection_limit");
  if (state->count) return JS_Throw(ctx, JS_DupValue(ctx, state->pending[0].reason));
  return JS_UNDEFINED;
}

static void study_free_rejections(JSContext *ctx) {
  StudyRejections *state = JS_GetContextOpaque(ctx);
  if (!state) return;
  JS_SetContextOpaque(ctx, NULL);
  while (state->count) study_remove_rejection(ctx, state, state->count - 1);
  js_free(ctx, state);
}

static int study_init_rejections(JSContext *ctx) {
  StudyRejections *state = js_mallocz(ctx, sizeof(*state));
  if (!state) return -1;
  JS_SetContextOpaque(ctx, state);
  JS_SetHostPromiseRejectionTracker(JS_GetRuntime(ctx), study_track_rejection, NULL);
  JSValue global = JS_GetGlobalObject(ctx);
  JSValue check = JS_NewCFunction(ctx, study_check_rejections, "__studyCheckRejections", 0);
  int result = JS_IsException(check) ? -1 : JS_SetPropertyStr(ctx, global, "__studyCheckRejections", check);
  JS_FreeValue(ctx, global);
  if (result < 0) study_free_rejections(ctx);
  return result < 0 ? -1 : 0;
}
