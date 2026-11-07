import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import "./style.css";
import App from "./App.vue";
import RunsView from "./views/RunsView.vue";
import RunDetailView from "./views/RunDetailView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/runs" },
    { path: "/runs", component: RunsView },
    { path: "/runs/:eventId", component: RunDetailView },
    { path: "/:pathMatch(.*)*", redirect: "/runs" },
  ],
});

createApp(App).use(router).mount("#app");
