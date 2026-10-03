/**
 * Streaming Slice — SSE Events & Real-time Updates
 *
 * Gestiona:
 * - Eventos de streaming (tokens, intents, plan creation)
 * - Actividad de herramientas
 * - Archivos generados
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '../store';

/**
 * Evento SSE `tool_activity` tal como lo emite el backend (router.py).
 * - calling  → tool, server?, args (cabeza ≤200 chars)
 * - result   → tool, server?, success, result_preview
 * - thinking → tool:"reasoning", detail (progreso textual)
 */
export interface ToolActivityEvent {
  activity: 'calling' | 'result' | 'thinking' | string;
  /** Agente que ejecuta la herramienta (`current_speaker` del backend). */
  agent?: string | null;
  tool: string;
  server?: string;
  success?: boolean;
  args?: string;
  result_preview?: string;
  detail?: string;
}

export type ToolActivity = ToolActivityEvent;

/** Deed persistido en `metadata.turn_log` por el backend (`_make_deed`). */
export interface TurnDeed {
  server: string;
  tool: string;
  status: 'success' | 'error' | string;
  agent?: string | null;
  args?: { text: string; chars: number; truncated: boolean };
  result?: { text: string; chars: number; truncated: boolean };
}

export interface GeneratedFile {
  file_id: string;
  filename: string;
  download_url: string;
  container_id?: string;
}

export interface StreamingState {
  currentIntent: string | null;
  currentConfidence: number | null;
  toolActivities: ToolActivity[];
  generatedFiles: GeneratedFile[];
  planCreatedId: string | null;
  lastEvent: any | null;
}

const initialState: StreamingState = {
  currentIntent: null,
  currentConfidence: null,
  toolActivities: [],
  generatedFiles: [],
  planCreatedId: null,
  lastEvent: null,
};

const streamingSlice = createSlice({
  name: 'streaming',
  initialState,
  reducers: {
    setIntent(state, action: PayloadAction<{ intent: string; confidence: number }>) {
      state.currentIntent = action.payload.intent;
      state.currentConfidence = action.payload.confidence;
    },

    addToolActivity(state, action: PayloadAction<ToolActivity>) {
      state.toolActivities.push(action.payload);
    },

    clearToolActivities(state) {
      state.toolActivities = [];
    },

    addGeneratedFile(state, action: PayloadAction<GeneratedFile>) {
      state.generatedFiles.push(action.payload);
    },

    clearGeneratedFiles(state) {
      state.generatedFiles = [];
    },

    setPlanCreated(state, action: PayloadAction<string | null>) {
      state.planCreatedId = action.payload;
    },

    setLastEvent(state, action: PayloadAction<any>) {
      state.lastEvent = action.payload;
    },

    reset(state) {
      state.currentIntent = null;
      state.currentConfidence = null;
      state.toolActivities = [];
      state.generatedFiles = [];
      state.planCreatedId = null;
      state.lastEvent = null;
    },
  },
});

export const {
  setIntent,
  addToolActivity,
  clearToolActivities,
  addGeneratedFile,
  clearGeneratedFiles,
  setPlanCreated,
  setLastEvent,
  reset,
} = streamingSlice.actions;

// Selectors
export const selectCurrentIntent = (state: RootState) => state.streaming.currentIntent;
export const selectCurrentConfidence = (state: RootState) => state.streaming.currentConfidence;
export const selectToolActivities = (state: RootState) => state.streaming.toolActivities;
export const selectGeneratedFiles = (state: RootState) => state.streaming.generatedFiles;
export const selectPlanCreatedId = (state: RootState) => state.streaming.planCreatedId;
export const selectLastEvent = (state: RootState) => state.streaming.lastEvent;

export default streamingSlice.reducer;
