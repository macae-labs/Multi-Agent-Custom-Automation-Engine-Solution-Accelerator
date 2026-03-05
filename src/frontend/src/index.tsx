import React, { StrictMode, useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';
import reportWebVitals from './reportWebVitals';
import { FluentProvider, teamsLightTheme, teamsDarkTheme } from "@fluentui/react-components";
import { setEnvData, setApiUrl, config as defaultConfig, toBoolean, getUserInfo, setUserInfoGlobal } from './api/config';
import { UserInfo } from './models';
const root = ReactDOM.createRoot(document.getElementById("root") as HTMLElement);

const AppWrapper = () => {
  // State to store the current theme
  const [isConfigLoaded, setIsConfigLoaded] = useState(false);
  const [isUserInfoLoaded, setIsUserInfoLoaded] = useState(false);
  const [isDarkMode, setIsDarkMode] = useState(
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
  type ConfigType = typeof defaultConfig;
  const [config, setConfig] = useState<ConfigType>(defaultConfig);
  useEffect(() => {
    const initConfig = async () => {
      // Load runtime config from frontend server (/config) and fallback to defaults.
      let runtimeConfig = { ...defaultConfig };
      try {
        const response = await fetch("/config");
        if (response.ok) {
          const configFromServer = await response.json();
          runtimeConfig = { ...runtimeConfig, ...configFromServer };
        }
      } catch {
        // keep default config in local/dev fallback
      }
      runtimeConfig.ENABLE_AUTH = toBoolean(runtimeConfig.ENABLE_AUTH as any);

      window.appConfig = runtimeConfig;
      setEnvData(runtimeConfig);
      setApiUrl(runtimeConfig.API_URL);
      setConfig(runtimeConfig);
      
      let defaultUserInfo = runtimeConfig.ENABLE_AUTH ? await getUserInfo() : ({} as UserInfo);
      window.userInfo = defaultUserInfo;
      setUserInfoGlobal(defaultUserInfo);
      
      setIsConfigLoaded(true);
      setIsUserInfoLoaded(true);
    };

    initConfig(); // Call the async function inside useEffect
  }, []);
  // Effect to listen for changes in the user's preferred color scheme
  useEffect(() => {
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");

    const handleThemeChange = (event: MediaQueryListEvent) => {
      setIsDarkMode(event.matches);
      document.body.classList.toggle("dark-mode", event.matches); // ✅ Add this
    };

    // Apply dark-mode class initially
    document.body.classList.toggle("dark-mode", isDarkMode);

    mediaQuery.addEventListener("change", handleThemeChange);
    return () => mediaQuery.removeEventListener("change", handleThemeChange);
  }, []);
  if (!isConfigLoaded || !isUserInfoLoaded) return <div>Loading...</div>;
  return (
    <StrictMode>
      <FluentProvider theme={isDarkMode ? teamsDarkTheme : teamsLightTheme} style={{ height: "100vh" }}>
        <App />
      </FluentProvider>
    </StrictMode>
  );
};
root.render(<AppWrapper />);
// If you want to start measuring performance in your app, pass a function
// to log results (for example: reportWebVitals(console.log))
// or send to an analytics endpoint. Learn more: https://bit.ly/CRA-vitals
reportWebVitals();
