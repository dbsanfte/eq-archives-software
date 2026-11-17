import React from "react";
import ReactDOM from "react-dom";
import App from "./App";
import { installArchiveFetch } from './search/ArchiveFetch';

installArchiveFetch();
ReactDOM.render(<App />, document.getElementById("root"));
