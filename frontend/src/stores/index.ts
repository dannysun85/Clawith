/** Global state management with Zustand */

import { create } from 'zustand';
import type { User, Agent } from '../types';
import { clearAuthStorage } from '../utils/authStorage';
import { clearBrowserSession, establishBrowserSession } from '../utils/authTransport';

interface AuthStore {
    user: User | null;
    token: string | null;
    setAuth: (user: User, token: string) => Promise<void>;
    setUser: (user: User) => void;
    logout: () => void;
    isAuthenticated: () => boolean;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
    user: null,
    token: localStorage.getItem('token'),

    setAuth: async (user, token) => {
        // Workspace media URLs intentionally carry no bearer token. Do not
        // expose authenticated UI until the HttpOnly browser credential is
        // confirmed, otherwise initial <img>/<video> requests can race it.
        await establishBrowserSession(token);
        localStorage.setItem('token', token);
        set({ user, token });
    },

    setUser: (user) => {
        set({ user });
    },

    logout: () => {
        clearBrowserSession();
        clearAuthStorage();
        set({ user: null, token: null });
        useAppStore.getState().setSelectedAgent(null);
    },

    isAuthenticated: () => !!get().token,
}));

interface AppStore {
    sidebarCollapsed: boolean;
    toggleSidebar: () => void;
    selectedAgentId: string | null;
    setSelectedAgent: (id: string | null) => void;
}

export const useAppStore = create<AppStore>((set) => ({
    sidebarCollapsed: localStorage.getItem('sidebar_collapsed') === 'true',
    toggleSidebar: () => set((s) => {
        const newState = !s.sidebarCollapsed;
        localStorage.setItem('sidebar_collapsed', String(newState));
        return { sidebarCollapsed: newState };
    }),
    selectedAgentId: null,
    setSelectedAgent: (id) => set({ selectedAgentId: id }),
}));
