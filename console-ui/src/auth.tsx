import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setCsrf, type SessionInfo } from "./api";

interface AuthState extends SessionInfo {
  loading: boolean;
}
interface AuthCtx extends AuthState {
  refresh: () => Promise<SessionInfo>;
  login: (token: string) => Promise<void>;
  bootstrapLogin: (token: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>(null as unknown as AuthCtx);
export const useAuth = () => useContext(Ctx);

const EMPTY: AuthState = {
  authenticated: false,
  setup_done: false,
  bootstrap_active: false,
  loading: true,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(EMPTY);

  const apply = useCallback((info: SessionInfo) => {
    if (info.csrf) setCsrf(info.csrf);
    setState({ ...EMPTY, ...info, loading: false });
    return info;
  }, []);

  const refresh = useCallback(async () => {
    try {
      return apply(await api.session());
    } catch {
      setState((s) => ({ ...s, loading: false }));
      return state;
    }
  }, [apply, state]);

  useEffect(() => {
    api
      .session()
      .then(apply)
      .catch(() => setState((s) => ({ ...s, loading: false })));
  }, [apply]);

  const login = useCallback(async (token: string) => void apply(await api.login(token)), [apply]);
  const bootstrapLogin = useCallback(
    async (token: string) => void apply(await api.bootstrapLogin(token)),
    [apply],
  );
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setCsrf("");
      await refresh();
    }
  }, [refresh]);

  return (
    <Ctx.Provider value={{ ...state, refresh, login, bootstrapLogin, logout }}>{children}</Ctx.Provider>
  );
}
