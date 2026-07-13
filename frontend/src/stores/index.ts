/** Global state management with Zustand */

import { create } from 'zustand';
import type { User, Agent } from '../types';
import { clearAuthStorage } from '../utils/authStorage';
import { clearBrowserSession, establishBrowserSession } from '../utils/authTransport';

interface AuthStore {
    user: User | null;
    token: string | null;
    setAuth: (user: User, token: string) => void;
    setUser: (user: User) => void;
    logout: () => void;
    isAuthenticated: () => boolean;
}

export const useAuthStore = create<AuthStore>((set, get) => ({
    user: null,
    token: localStorage.getItem('token'),

    setAuth: (user, token) => {
        localStorage.setItem('token', token);
        establishBrowserSession(token);
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
