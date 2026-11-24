<script setup lang="ts">
import { RouterLink } from 'vue-router'

const plannedChannels = [
  { key: 'feishu', name: '飞书', mark: '飞', description: '团队消息与 Agent 对话' },
  { key: 'qq', name: 'QQ', mark: 'QQ', description: 'QQ 聊天消息' },
  { key: 'dingtalk', name: '钉钉', mark: '钉', description: '企业协作消息' },
]
</script>

<template>
  <section
    class="channel-directory"
    aria-labelledby="channel-directory-title"
  >
    <header class="channel-directory__header">
      <h1 id="channel-directory-title">
        通讯通道
      </h1>
      <p>在常用聊天应用中与 Agent 对话、接收任务通知。</p>
    </header>

    <article
      class="channel-directory__featured"
      aria-labelledby="channel-wechat-title"
    >
      <span
        class="channel-directory__mark channel-directory__mark--wechat"
        aria-hidden="true"
      >微</span>
      <div class="channel-directory__summary">
        <h2 id="channel-wechat-title">
          微信
        </h2>
        <p>绑定个人微信，与 Agent 一对一对话，并接收已开启推送的定时任务结果。</p>
      </div>
      <RouterLink
        class="channel-directory__action"
        to="/channels/wechat"
      >
        管理微信 <span aria-hidden="true">↗</span>
      </RouterLink>
    </article>

    <section aria-labelledby="channel-planned-title">
      <h2
        id="channel-planned-title"
        class="channel-directory__section-title"
      >
        规划中的通道
      </h2>
      <ul class="channel-directory__list">
        <li
          v-for="channel in plannedChannels"
          :key="channel.key"
          class="channel-directory__item"
        >
          <span
            class="channel-directory__mark"
            :class="'channel-directory__mark--' + channel.key"
            aria-hidden="true"
          >{{ channel.mark }}</span>
          <div class="channel-directory__summary">
            <h3>{{ channel.name }}</h3>
            <p>{{ channel.description }}</p>
          </div>
        </li>
      </ul>
    </section>
  </section>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.channel-directory {
  max-width: 1180px;
  margin-inline: auto;
  padding: 1.25rem clamp(1rem, 3.4vw, 2.25rem) 2rem;
  color: $ink;

  &__header {
    margin-bottom: 1.25rem;
    h1 { @include page-title; margin: 0; }
    p { margin: .45rem 0 0; color: $muted; }
  }

  &__featured, &__item {
    display: flex;
    align-items: center;
    min-width: 0;
    gap: .8rem;
    border: 1px solid $line;
    border-radius: $panel-radius;
  }

  &__featured {
    flex-wrap: wrap;
    padding: clamp(1rem, 2.6vw, 1.5rem);
    background: linear-gradient(112deg, $hero-glow, $hero);
  }

  &__summary {
    flex: 1;
    min-width: 0;
    h2, h3 { margin: 0; line-height: 1.3; }
    h2 { font-size: 1.35rem; }
    h3 { font-size: 1rem; }
    p { margin: .25rem 0 0; color: $muted; font-size: .85rem; overflow-wrap: anywhere; }
  }

  &__featured &__summary { flex-basis: min(100%, 15rem); }

  &__mark {
    display: grid;
    place-items: center;
    flex: 0 0 2.35rem;
    height: 2.35rem;
    border-radius: 8px;
    background: $sidebar;
    color: $ink;
    font-size: .9rem;
    font-weight: 700;

    &--wechat { flex-basis: 2.75rem; height: 2.75rem; background: $green; color: $on-green; }
    &--feishu { background: #e5ecfa; color: #2a4e85; }
    &--qq { background: #e6eff5; color: #34596d; }
    &--dingtalk { background: #e9ebf8; color: #464d8a; }
  }

  &__action {
    display: inline-flex;
    align-items: center;
    gap: .35rem;
    min-height: 2.5rem;
    padding: .4rem .75rem;
    border-radius: $control-radius;
    background: $green;
    color: $on-green;
    font-weight: 650;
    text-decoration: none;
    &:hover { background: $green-hover; }
  }

  &__section-title { margin: 1.4rem 0 .65rem; font-size: 1.1rem; }
  &__list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 13rem), 1fr));
    gap: .65rem;
    padding: 0;
    margin: 0;
    list-style: none;
  }
  &__item { padding: .85rem; background: $surface; }
}
</style>
